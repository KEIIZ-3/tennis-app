from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.models import (
    CoachAvailability,
    Court,
    Reservation,
    TicketCashReceipt,
    TicketConsumption,
    TicketLedger,
    TicketPurchase,
    purchase_tickets,
)
from club.settlement_models import MonthlySettlement
from club.ticket_purchase_correction_service import correct_ticket_purchase


class TicketPurchaseCorrectionTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.admin = users.objects.create_superuser("correction-admin", "a@example.com", "pw")
        self.coach = users.objects.create_user("correction-coach", role=users.ROLE_COACH)
        self.member = users.objects.create_user("correction-member", full_name="修正会員")
        self.court = Court.objects.create(name="Correction court")
        self.base = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=10), time(10))
        )
        self.token_counter = 0

    def grant(self, tickets=4, price=3500, *, cash=False, purchased_at=None):
        return purchase_tickets(
            user=self.member,
            tickets=tickets,
            unit_price=price,
            purchase_type=(
                TicketPurchase.PURCHASE_TYPE_SET4
                if tickets == 4
                else TicketPurchase.PURCHASE_TYPE_SINGLE
            ),
            reason=(
                TicketLedger.REASON_PURCHASE_SET4
                if tickets == 4
                else TicketLedger.REASON_PURCHASE_SINGLE
            ),
            created_by=self.admin,
            purchased_at=purchased_at or self.base - timedelta(days=2),
            cash_received_at=(purchased_at or self.base - timedelta(days=2)) if cash else None,
        )[1]

    def reserve_and_consume(self, tickets=1, offset=0):
        start = self.base + timedelta(hours=offset)
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            start_at=start,
            end_at=start + timedelta(hours=2),
            capacity=20,
        )
        reservation = Reservation.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court,
            availability=availability,
            start_at=start,
            end_at=start + timedelta(hours=2),
            tickets_used=tickets,
        )
        Reservation.objects.filter(pk=reservation.pk).update(tickets_used=tickets)
        reservation.tickets_used = tickets
        reservation.consume_tickets()
        reservation.refresh_from_db()
        return reservation

    def correct(self, purchase, **overrides):
        self.token_counter += 1
        values = {
            "purchase_id": purchase.pk,
            "actor": self.admin,
            "tickets": purchase.total_tickets,
            "unit_price": purchase.unit_price,
            "purchase_type": purchase.purchase_type,
            "purchased_at": purchase.purchased_at,
            "note": "修正後メモ",
            "reason": "登録内容を確認して訂正",
            "idempotency_key": f"00000000-0000-4000-8000-{self.token_counter:012d}",
            "cash_mode": "none",
        }
        values.update(overrides)
        return correct_ticket_purchase(**values)

    def test_unused_single_price_correction_keeps_auditable_pair(self):
        original = self.grant(tickets=1, price=4000)
        replacement, changed = self.correct(original, unit_price=3500)

        original.refresh_from_db()
        self.member.refresh_from_db()
        self.assertTrue(changed)
        self.assertIsNotNone(original.reversed_at)
        self.assertEqual(original.reversed_by, self.admin)
        self.assertEqual(replacement.corrected_from, original)
        self.assertEqual(replacement.correction_reason, "登録内容を確認して訂正")
        self.assertEqual((replacement.total_tickets, replacement.unit_price, replacement.remaining_tickets), (1, 3500, 1))
        self.assertEqual(self.member.ticket_balance, 1)

    def test_unused_set_can_change_count_and_price_without_double_ledger(self):
        original = self.grant()
        replacement, _ = self.correct(
            original,
            tickets=2,
            unit_price=4000,
            purchase_type=TicketPurchase.PURCHASE_TYPE_ADMIN,
        )

        self.member.refresh_from_db()
        self.assertEqual(self.member.ticket_balance, 2)
        self.assertEqual(replacement.remaining_tickets, 2)
        self.assertEqual(
            list(TicketLedger.objects.values_list("change_amount", flat=True).order_by("id")),
            [4, -4, 2],
        )

    def test_consumed_purchase_is_reallocated_fifo_and_reservation_count_is_unchanged(self):
        original = self.grant()
        reservation = self.reserve_and_consume(tickets=2)
        old_consumption = TicketConsumption.objects.get(reservation=reservation)

        replacement, _ = self.correct(original, unit_price=4000)

        reservation.refresh_from_db()
        old_consumption.refresh_from_db()
        active = TicketConsumption.objects.get(reservation=reservation, refunded_at__isnull=True)
        self.member.refresh_from_db()
        self.assertEqual(reservation.tickets_used, 2)
        self.assertIsNotNone(old_consumption.refunded_at)
        self.assertEqual((active.purchase_id, active.unit_price_snapshot), (replacement.pk, 4000))
        self.assertEqual(reservation.participant_ticket_price_snapshot, 8000)
        self.assertEqual(replacement.remaining_tickets, 2)
        self.assertEqual(self.member.ticket_balance, 2)

    def test_fifo_rebuild_can_shift_later_lot_and_preserves_canceled_history(self):
        first = self.grant(tickets=1, price=3500)
        second = self.grant(tickets=4, price=4000, purchased_at=self.base - timedelta(days=1))
        canceled = self.reserve_and_consume(offset=0)
        canceled.cancel()
        active = self.reserve_and_consume(offset=3)
        self.correct(first, unit_price=3000)

        active.refresh_from_db()
        current = TicketConsumption.objects.get(reservation=active, refunded_at__isnull=True)
        self.assertEqual(current.purchase.corrected_from_id, first.pk)
        self.assertEqual(active.participant_ticket_price_snapshot, 3000)
        self.assertFalse(TicketConsumption.objects.filter(reservation=canceled, refunded_at__isnull=True).exists())
        second.refresh_from_db()
        self.assertEqual(second.remaining_tickets, 4)

    def test_cash_receipt_is_recreated_and_amount_change_must_be_explicit(self):
        original = self.grant(cash=True)
        with self.assertRaises(ValidationError):
            self.correct(original, tickets=1, unit_price=4000, cash_mode="none")

        receipt = TicketCashReceipt.objects.get(ticket_purchase=original)
        replacement, _ = self.correct(
            original,
            tickets=1,
            unit_price=4000,
            cash_mode="replace",
            cash_amount=4000,
            cash_received_at=receipt.received_at,
        )
        receipt.refresh_from_db()
        new_receipt = TicketCashReceipt.objects.get(ticket_purchase=replacement)
        self.assertIsNotNone(receipt.reversed_at)
        self.assertEqual(new_receipt.amount, 4000)

    def test_cash_receipt_can_be_preserved_with_same_amount_and_date(self):
        original = self.grant(cash=True)
        receipt = TicketCashReceipt.objects.get(ticket_purchase=original)
        replacement, _ = self.correct(original, unit_price=4000, cash_mode="preserve")
        recreated = TicketCashReceipt.objects.get(ticket_purchase=replacement)
        self.assertEqual((recreated.amount, recreated.received_at), (receipt.amount, receipt.received_at))

    def test_closed_purchase_or_consumption_month_is_rejected_atomically(self):
        original = self.grant()
        MonthlySettlement.objects.create(
            year=original.purchased_at.year,
            month=original.purchased_at.month,
            status=MonthlySettlement.STATUS_CLOSED,
        )
        before = list(TicketLedger.objects.values_list("id", "change_amount"))
        with self.assertRaises(ValidationError):
            self.correct(original, unit_price=4000)
        original.refresh_from_db()
        self.assertIsNone(original.reversed_at)
        self.assertEqual(list(TicketLedger.objects.values_list("id", "change_amount")), before)

    def test_only_admin_can_correct_and_repeated_confirmation_is_idempotent(self):
        original = self.grant()
        with self.assertRaises(PermissionDenied):
            self.correct(original, actor=self.member)
        with self.assertRaises(PermissionDenied):
            self.correct(original, actor=self.coach)

        token = "99999999-9999-4999-8999-999999999999"
        first, changed = self.correct(original, idempotency_key=token)
        second, changed_again = self.correct(original, idempotency_key=token)
        self.assertTrue(changed)
        self.assertFalse(changed_again)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(TicketPurchase.objects.filter(user=self.member).count(), 2)
        self.assertEqual(TicketLedger.objects.filter(user=self.member).count(), 3)

    def test_admin_ui_has_two_step_confirmation_and_non_admin_cannot_open_it(self):
        original = self.grant()
        url = reverse("admin:club_ticketpurchase_correct", args=[original.pk])
        self.client.force_login(self.admin)
        response = self.client.get(url)
        self.assertContains(response, "確認画面へ")
        token = response.context["form"]["idempotency_key"].value()
        data = {
            "tickets": 4,
            "unit_price": 4000,
            "purchase_type": TicketPurchase.PURCHASE_TYPE_SET4,
            "purchased_at": timezone.localtime(original.purchased_at).strftime("%Y-%m-%dT%H:%M"),
            "note": "UI修正",
            "correction_reason": "入力誤り",
            "cash_mode": "none",
            "idempotency_key": token,
        }
        preview = self.client.post(url, data)
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, "最終確認")
        self.assertEqual(TicketPurchase.objects.count(), 1)

        self.client.force_login(self.coach)
        self.assertEqual(self.client.get(url).status_code, 302)
