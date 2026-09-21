import json
from datetime import date, datetime, timedelta
from io import StringIO
from unittest.mock import call, patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from club.legacy_ticket_accounting_repair import (
    apply_legacy_ticket_accounting,
    inspect_legacy_ticket_accounting,
)
from club.models import Court, Reservation, TicketConsumption, TicketLedger, TicketPurchase, User
from club.settlement_models import MonthlySettlement


class LegacyTicketAccountingRepairTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user(username="legacy-member", full_name="Legacy Member")
        self.coach = User.objects.create_user(username="legacy-coach", full_name="Legacy Coach", role=User.ROLE_COACH)
        self.court = Court.objects.create(name="legacy court")

    def at(self, year, month, day, hour=10):
        return timezone.make_aware(datetime(year, month, day, hour))

    def purchase(self, when, *, tickets=4, remaining=None, price=3500, purchase_type=TicketPurchase.PURCHASE_TYPE_SET4):
        return TicketPurchase.objects.create(
            user=self.member, purchased_at=when, total_tickets=tickets,
            remaining_tickets=tickets if remaining is None else remaining,
            unit_price=price, purchase_type=purchase_type,
        )

    def reservation(self, when, *, snapshot=None, status=Reservation.STATUS_ACTIVE, tickets=1):
        row = Reservation(
            user=self.member, coach=self.coach, court=self.court,
            start_at=when, end_at=when + timedelta(hours=1), tickets_used=tickets,
            participant_ticket_price_snapshot=snapshot, status=status,
            lesson_type=Reservation.LESSON_PRIVATE,
        )
        Reservation.objects.bulk_create([row])
        Reservation.objects.filter(pk=row.pk).update(ticket_consumed_at=when)
        row.refresh_from_db()
        ledger = TicketLedger.objects.create(
            user=self.member, reservation=row, change_amount=-tickets,
            balance_after=self.member.ticket_balance, reason=TicketLedger.REASON_RESERVATION_USE,
        )
        TicketLedger.objects.filter(pk=ledger.pk).update(created_at=when)
        return row

    def guest_reservation(self, when, *, snapshot=2300):
        row = Reservation(
            user=None, guest_name="Legacy Guest", coach=self.coach, court=self.court,
            start_at=when, end_at=when + timedelta(hours=1), tickets_used=1,
            participant_ticket_price_snapshot=snapshot, status=Reservation.STATUS_ACTIVE,
            lesson_type=Reservation.LESSON_PRIVATE,
        )
        Reservation.objects.bulk_create([row])
        return row

    def set_balance(self, value):
        self.member.ticket_balance = value
        self.member.save(update_fields=["ticket_balance"])

    def inspect(self, **kwargs):
        return inspect_legacy_ticket_accounting(
            from_date=kwargs.pop("from_date", date(2026, 8, 1)), **kwargs
        )

    def test_default_scope_starts_in_august_and_custom_scope_excludes_april_and_may(self):
        purchase = self.purchase(self.at(2026, 4, 1), tickets=3, remaining=0)
        april = self.reservation(self.at(2026, 4, 10))
        may = self.reservation(self.at(2026, 5, 10))
        august = self.reservation(self.at(2026, 8, 10))
        self.set_balance(0)
        self.assertEqual([row.reservation_id for row in self.inspect()], [august.id])
        rows = self.inspect(from_date=date(2026, 4, 1))
        status = {row.reservation_id: row.repair_status for row in rows}
        self.assertEqual(status[april.id], "excluded_test_period")
        self.assertEqual(status[may.id], "excluded_test_period")
        self.assertNotEqual(status[august.id], "excluded_test_period")

    def test_guest_reservations_are_excluded_from_inspection_and_apply(self):
        guest = self.guest_reservation(self.at(2026, 8, 5))
        self.purchase(self.at(2026, 8, 1), tickets=1, remaining=0)
        member = self.reservation(self.at(2026, 8, 10))
        self.set_balance(0)

        rows = self.inspect()

        self.assertEqual([row.reservation_id for row in rows], [member.id])
        self.assertEqual(rows[0].repair_status, "repairable")

        apply_legacy_ticket_accounting(from_date=date(2026, 8, 1))

        guest.refresh_from_db()
        self.assertIsNone(guest.user_id)
        self.assertEqual(guest.guest_name, "Legacy Guest")
        self.assertEqual(guest.tickets_used, 1)
        self.assertEqual(guest.participant_ticket_price_snapshot, 2300)
        self.assertFalse(TicketConsumption.objects.filter(reservation=guest).exists())

    def test_manual_balance_history_remains_ambiguous(self):
        self.purchase(self.at(2026, 8, 1), tickets=1, remaining=0)
        reservation = self.reservation(self.at(2026, 8, 10))
        adjustment = TicketLedger.objects.create(
            user=self.member, change_amount=0, balance_after=0,
            reason=TicketLedger.REASON_ADMIN_ADJUST,
        )
        TicketLedger.objects.filter(pk=adjustment.pk).update(created_at=self.at(2026, 8, 11))
        self.set_balance(0)

        row = self.inspect(reservation_ids=[reservation.id])[0]

        self.assertEqual((row.repair_status, row.reason), ("ambiguous", "manual_balance_history"))

    def test_fifo_repairs_missing_consumption_and_snapshot_only_on_apply(self):
        first = self.purchase(self.at(2026, 8, 1), tickets=2, remaining=0, price=3500)
        second = self.purchase(self.at(2026, 9, 1), tickets=2, remaining=2, price=4000)
        reservation = self.reservation(self.at(2026, 9, 20))
        earlier = self.reservation(self.at(2026, 8, 20))
        self.set_balance(2)
        rows = self.inspect(reservation_ids=[reservation.id])
        self.assertEqual(rows[0].candidate_purchases, [{"purchase_id": first.id, "tickets_used": 1, "unit_price": 3500}])
        self.assertEqual(rows[0].repair_status, "repairable")
        self.assertFalse(TicketConsumption.objects.filter(reservation=reservation).exists())
        apply_legacy_ticket_accounting(reservation_ids=[reservation.id])
        reservation.refresh_from_db()
        consumption = TicketConsumption.objects.get(reservation=reservation)
        self.assertEqual((consumption.purchase_id, consumption.unit_price_snapshot), (first.id, 3500))
        self.assertEqual(reservation.participant_ticket_price_snapshot, 3500)
        self.assertFalse(TicketConsumption.objects.filter(reservation=earlier).exists())

    def test_repairs_existing_null_and_zero_consumptions_when_fifo_is_unique(self):
        purchase = self.purchase(self.at(2026, 8, 1), tickets=2, remaining=0, price=3500)
        null_row = self.reservation(self.at(2026, 8, 10))
        zero_row = self.reservation(self.at(2026, 8, 20))
        TicketConsumption.objects.create(user=self.member, reservation=null_row, tickets_used=1)
        TicketConsumption.objects.create(user=self.member, reservation=zero_row, purchase=purchase, tickets_used=1, unit_price_snapshot=0)
        self.set_balance(0)
        apply_legacy_ticket_accounting(reservation_ids=[null_row.id, zero_row.id])
        self.assertEqual(
            list(TicketConsumption.objects.order_by("reservation__start_at").values_list("purchase_id", "unit_price_snapshot")),
            [(purchase.id, 3500), (purchase.id, 3500)],
        )

    def test_unproven_zero_price_and_unknown_balance_are_ambiguous(self):
        self.purchase(self.at(2026, 8, 1), tickets=1, remaining=0, price=0, purchase_type=TicketPurchase.PURCHASE_TYPE_LEGACY)
        reservation = self.reservation(self.at(2026, 8, 20))
        self.set_balance(0)
        self.assertEqual(self.inspect()[0].reason, "zero_price_purchase_not_proven_free")
        TicketPurchase.objects.all().delete()
        self.set_balance(1)
        self.assertEqual(self.inspect()[0].reason, "purchase_history_insufficient")

    def test_formal_free_ticket_is_unambiguous_zero_price(self):
        purchase = self.purchase(
            self.at(2026, 8, 1), tickets=1, remaining=0, price=0,
            purchase_type=TicketPurchase.PURCHASE_TYPE_FORMAL_FREE,
        )
        reservation = self.reservation(self.at(2026, 8, 20))
        self.set_balance(0)
        row = self.inspect()[0]
        self.assertEqual((row.repair_status, row.expected_snapshot), ("repairable", 0))
        self.assertEqual(row.candidate_purchases[0]["purchase_id"], purchase.id)

    def test_split_fifo_allocation_sets_snapshot_to_consumption_total(self):
        first = self.purchase(self.at(2026, 8, 1), tickets=1, remaining=0, price=3500)
        second = self.purchase(self.at(2026, 8, 2), tickets=2, remaining=1, price=4000)
        reservation = self.reservation(self.at(2026, 8, 20), tickets=2)
        self.set_balance(1)
        apply_legacy_ticket_accounting(reservation_ids=[reservation.id])
        reservation.refresh_from_db()
        self.assertEqual(
            list(reservation.ticket_consumptions.order_by("purchase__purchased_at").values_list("purchase_id", "tickets_used", "unit_price_snapshot")),
            [(first.id, 1, 3500), (second.id, 1, 4000)],
        )
        self.assertEqual(reservation.participant_ticket_price_snapshot, 7500)

    def test_duplicate_use_ledger_is_ambiguous(self):
        self.purchase(self.at(2026, 8, 1), tickets=2, remaining=0)
        reservation = self.reservation(self.at(2026, 8, 20))
        duplicate = TicketLedger.objects.create(
            user=self.member, reservation=reservation, change_amount=-1,
            balance_after=0, reason=TicketLedger.REASON_RESERVATION_USE,
        )
        TicketLedger.objects.filter(pk=duplicate.pk).update(created_at=self.at(2026, 8, 20, 11))
        self.set_balance(0)
        self.assertEqual(self.inspect()[0].repair_status, "ambiguous")

    def test_closed_month_is_skipped_and_normal_reservation_is_unchanged(self):
        purchase = self.purchase(self.at(2026, 8, 1), tickets=2, remaining=0)
        closed = self.reservation(self.at(2026, 8, 10))
        normal = self.reservation(self.at(2026, 9, 10), snapshot=3500)
        TicketConsumption.objects.create(user=self.member, reservation=normal, purchase=purchase, tickets_used=1, unit_price_snapshot=3500)
        self.set_balance(0)
        MonthlySettlement.objects.create(year=2026, month=8, status=MonthlySettlement.STATUS_CLOSED)
        statuses = {row.reservation_id: row.repair_status for row in self.inspect()}
        self.assertEqual(statuses[closed.id], "skipped_closed_month")
        self.assertEqual(statuses[normal.id], "already_ok")

    def test_refunded_reservation_is_not_treated_as_active_consumption(self):
        purchase = self.purchase(self.at(2026, 8, 1), tickets=1, remaining=1)
        reservation = self.reservation(self.at(2026, 8, 10), status=Reservation.STATUS_CANCELED)
        refund_at = self.at(2026, 8, 11)
        Reservation.objects.filter(pk=reservation.pk).update(ticket_refunded_at=refund_at)
        refund = TicketLedger.objects.create(
            user=self.member, reservation=reservation, change_amount=1,
            balance_after=1, reason=TicketLedger.REASON_CANCEL_REFUND,
        )
        TicketLedger.objects.filter(pk=refund.pk).update(created_at=refund_at)
        self.set_balance(1)
        row = self.inspect()[0]
        self.assertEqual((row.repair_status, row.reason), ("ambiguous", "reservation_not_active_consumption"))
        self.assertEqual(purchase.remaining_tickets, 1)

    def test_dry_run_command_writes_nothing_and_reports_required_fields(self):
        self.purchase(self.at(2026, 8, 1), tickets=1, remaining=0)
        reservation = self.reservation(self.at(2026, 9, 20))
        self.set_balance(0)
        stream = StringIO()
        call_command("repair_legacy_ticket_accounting", reservation_id=[reservation.id], stdout=stream)
        payload = json.loads(stream.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["rows"][0]["repair_status"], "repairable")
        self.assertFalse(TicketConsumption.objects.filter(reservation=reservation).exists())
        for key in ("reservation_id", "lesson_date", "member_name", "coach_name", "tickets_used", "current_snapshot", "current_consumptions", "candidate_purchases", "candidate_unit_prices", "reason", "repair_status"):
            self.assertIn(key, payload["rows"][0])

    @patch("club.settlement_service.calculate_monthly_settlement")
    def test_apply_recalculates_each_changed_month_with_canonical_chain(self, calculate):
        self.purchase(self.at(2026, 8, 1), tickets=2, remaining=0)
        august = self.reservation(self.at(2026, 8, 20))
        september = self.reservation(self.at(2026, 9, 20))
        self.set_balance(0)
        MonthlySettlement.objects.create(year=2026, month=8)
        MonthlySettlement.objects.create(year=2026, month=9)
        apply_legacy_ticket_accounting(reservation_ids=[august.id, september.id])
        self.assertEqual(calculate.call_args_list, [
            call(2026, 8, force=True), call(2026, 9, force=True)
        ])
