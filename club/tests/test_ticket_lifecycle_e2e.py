from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.models import (
    Court,
    CoachAvailability,
    FixedLesson,
    Reservation,
    TicketConsumption,
    TicketLedger,
    TicketPurchase,
    purchase_tickets,
)


class TicketLifecycleE2ETests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.member = user_model.objects.create_user(username="ticket-owner")
        self.coach = user_model.objects.create_user(
            username="ticket-coach", role=user_model.ROLE_COACH
        )
        self.court = Court.objects.create(name="Ticket lifecycle court")
        self.start_at = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=7), time(10))
        )
        self.availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            start_at=self.start_at,
            end_at=self.start_at + timedelta(hours=2),
            capacity=20,
        )

    def purchase(self, tickets=4, unit_price=3500, purchased_at=None):
        return purchase_tickets(
            user=self.member,
            tickets=tickets,
            unit_price=unit_price,
            purchase_type=TicketPurchase.PURCHASE_TYPE_SET4,
            reason=TicketLedger.REASON_PURCHASE_SET4,
            purchased_at=purchased_at,
        )

    def reservation(self, *, tickets=1, fixed_lesson=None, hour_offset=0):
        start_at = self.start_at + timedelta(hours=hour_offset)
        availability = self.availability
        if hour_offset:
            availability = CoachAvailability.objects.create(
                coach=self.coach,
                court=self.court,
                start_at=start_at,
                end_at=start_at + timedelta(hours=2),
                capacity=20,
            )
        reservation = Reservation.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court,
            availability=availability,
            fixed_lesson=fixed_lesson,
            is_fixed_entry=fixed_lesson is not None,
            start_at=start_at,
            end_at=start_at + timedelta(hours=2),
            tickets_used=tickets,
        )
        Reservation.objects.filter(pk=reservation.pk).update(tickets_used=tickets)
        reservation.tickets_used = tickets
        return reservation

    def ticket_summary(self):
        self.client.force_login(self.coach)
        return self.client.get(
            reverse("club:coach_ticket_summary"),
            {"year": self.start_at.year, "month": self.start_at.month},
        )

    def test_purchase_consume_invariants(self):
        purchase_ledger, lot = self.purchase()
        reservation = self.reservation()
        use_ledger = reservation.consume_tickets()

        self.member.refresh_from_db()
        lot.refresh_from_db()
        reservation.refresh_from_db()
        consumption = TicketConsumption.objects.get(reservation=reservation)
        self.assertEqual((self.member.ticket_balance, lot.remaining_tickets), (3, 3))
        self.assertEqual((purchase_ledger.change_amount, purchase_ledger.balance_after), (4, 4))
        self.assertEqual((use_ledger.change_amount, use_ledger.balance_after), (-1, 3))
        self.assertEqual(consumption.tickets_used, reservation.tickets_used)
        self.assertEqual(consumption.unit_price_snapshot, 3500)
        self.assertIsNotNone(reservation.ticket_consumed_at)
        self.assertEqual(reservation.participant_ticket_price_snapshot, 3500)

    def test_cancel_rebook_retains_history_and_snapshot(self):
        _, lot = self.purchase()
        first = self.reservation()
        first.consume_tickets()
        first.cancel()
        original_snapshot = first.participant_ticket_price_snapshot
        second = self.reservation()
        second.consume_tickets()

        self.member.refresh_from_db()
        lot.refresh_from_db()
        first.refresh_from_db()
        rows = list(TicketConsumption.objects.order_by("id"))
        self.assertEqual((self.member.ticket_balance, lot.remaining_tickets), (3, 3))
        self.assertEqual(len(rows), 2)
        self.assertIsNotNone(rows[0].refunded_at)
        self.assertIsNone(rows[1].refunded_at)
        self.assertIsNotNone(first.ticket_refunded_at)
        self.assertEqual(first.participant_ticket_price_snapshot, original_snapshot)
        self.assertEqual(
            list(TicketLedger.objects.values_list("change_amount", flat=True).order_by("id")),
            [4, -1, 1, -1],
        )

    def test_fifo_uses_saved_lot_prices(self):
        old = self.start_at - timedelta(days=2)
        _, lot_a = self.purchase(tickets=1, unit_price=3500, purchased_at=old)
        _, lot_b = self.purchase(tickets=4, unit_price=4000, purchased_at=old + timedelta(days=1))
        reservation = self.reservation(tickets=2)
        reservation.consume_tickets()
        reservation.refresh_from_db()

        rows = list(TicketConsumption.objects.filter(reservation=reservation).order_by("id"))
        self.assertEqual([(row.purchase_id, row.tickets_used) for row in rows], [(lot_a.pk, 1), (lot_b.pk, 1)])
        self.assertEqual(reservation.participant_ticket_price_snapshot, 7500)

    def test_legacy_balance_without_purchase_is_consumed_without_synthetic_evidence(self):
        self.member.ticket_balance = 2
        self.member.save(update_fields=["ticket_balance"])
        reservation = self.reservation()

        use_ledger = reservation.consume_tickets()
        reservation.refresh_from_db()
        self.member.refresh_from_db()

        self.assertEqual((use_ledger.change_amount, use_ledger.balance_after), (-1, 1))
        self.assertEqual(self.member.ticket_balance, 1)
        self.assertIsNotNone(reservation.ticket_consumed_at)
        self.assertIsNone(reservation.participant_ticket_price_snapshot)
        self.assertFalse(TicketPurchase.objects.exists())
        self.assertFalse(TicketConsumption.objects.exists())
        self.assertFalse(
            TicketPurchase.objects.filter(
                purchase_type=TicketPurchase.PURCHASE_TYPE_LEGACY,
                unit_price=0,
                label="旧データ移行分",
            ).exists()
        )

        reservation.cancel()
        self.member.refresh_from_db()
        self.assertEqual(self.member.ticket_balance, 2)
        self.assertFalse(TicketPurchase.objects.exists())
        self.assertFalse(TicketConsumption.objects.exists())

    def test_partial_purchase_evidence_consumes_only_proven_lot_and_keeps_price_unknown(self):
        _, lot = self.purchase(tickets=1, unit_price=3500)
        self.member.ticket_balance = 2
        self.member.save(update_fields=["ticket_balance"])
        reservation = self.reservation(tickets=2)

        reservation.consume_tickets()
        reservation.refresh_from_db()
        self.member.refresh_from_db()
        lot.refresh_from_db()
        consumption = TicketConsumption.objects.get(reservation=reservation)

        self.assertEqual((self.member.ticket_balance, lot.remaining_tickets), (0, 0))
        self.assertEqual((consumption.tickets_used, consumption.unit_price_snapshot), (1, 3500))
        self.assertIsNone(reservation.participant_ticket_price_snapshot)
        self.assertEqual(TicketPurchase.objects.count(), 1)

        reservation.cancel()
        self.member.refresh_from_db()
        lot.refresh_from_db()
        consumption.refresh_from_db()
        self.assertEqual((self.member.ticket_balance, lot.remaining_tickets), (2, 1))
        self.assertIsNotNone(consumption.refunded_at)
        self.assertEqual(TicketPurchase.objects.count(), 1)

    def test_ticket_summary_keeps_historical_active_consumption_without_marker(self):
        _, lot = self.purchase(tickets=1, unit_price=4200)
        reservation = self.reservation()
        TicketConsumption.objects.create(
            user=self.member,
            purchase=lot,
            reservation=reservation,
            tickets_used=1,
            unit_price_snapshot=4200,
        )

        response = self.ticket_summary()

        self.assertEqual(response.context["total_tickets"], 1)
        self.assertEqual(response.context["total_amount"], 4200)

    def test_ticket_summary_reports_fully_and_partially_unknown_consumption(self):
        self.member.ticket_balance = 2
        self.member.save(update_fields=["ticket_balance"])
        fully_unknown = self.reservation()
        fully_unknown.consume_tickets()

        _, lot = self.purchase(tickets=1, unit_price=3600)
        partially_unknown = self.reservation(tickets=2, hour_offset=3)
        partially_unknown.consume_tickets()

        response = self.ticket_summary()

        self.assertEqual(response.context["total_tickets"], 3)
        self.assertEqual(response.context["total_amount"], 3600)
        self.assertEqual(
            [(row["unit_price"], row["tickets"]) for row in response.context["breakdown_rows"]],
            [(0, 2), (3600, 1)],
        )

    def test_ticket_summary_does_not_revive_refunded_or_canceled_evidence_as_unknown(self):
        _, lot = self.purchase(tickets=2, unit_price=3500)
        refunded = self.reservation(tickets=2)
        refunded.consume_tickets()
        refunded.cancel()

        canceled = self.reservation(hour_offset=3)
        canceled.consume_tickets()
        Reservation.objects.filter(pk=canceled.pk).update(
            status=Reservation.STATUS_RAIN_CANCELED,
            ticket_refunded_at=None,
        )

        response = self.ticket_summary()

        self.assertEqual(response.context["total_tickets"], 0)
        self.assertEqual(response.context["total_amount"], 0)

    def test_zero_ticket_reservation_does_not_create_ticket_state(self):
        self.purchase()
        reservation = self.reservation(tickets=0)
        self.assertIsNone(reservation.consume_tickets())
        reservation.refresh_from_db()
        self.member.refresh_from_db()
        self.assertEqual(self.member.ticket_balance, 4)
        self.assertIsNone(reservation.ticket_consumed_at)
        self.assertIsNone(reservation.participant_ticket_price_snapshot)
        self.assertFalse(TicketConsumption.objects.exists())
        self.assertEqual(TicketLedger.objects.count(), 1)

    def test_family_contact_account_and_fixed_lesson_use_same_owner(self):
        self.purchase(tickets=4)
        fixed = FixedLesson.objects.create(
            title="Ticket fixed lesson",
            coach=self.coach,
            court=self.court,
            start_date=self.start_at.date(),
            weekday=self.start_at.weekday(),
            start_hour=self.start_at.hour,
        )
        family_reservation = self.reservation()
        family_reservation.consume_tickets()
        family_reservation.cancel()
        fixed_reservation = self.reservation(fixed_lesson=fixed)
        fixed_reservation.consume_tickets(reason=TicketLedger.REASON_FIXED_USE)
        self.assertEqual(
            set(TicketConsumption.objects.values_list("user_id", flat=True)),
            {self.member.pk},
        )
        self.assertEqual(
            set(TicketLedger.objects.values_list("user_id", flat=True)), {self.member.pk}
        )
        self.assertNotEqual(
            TicketConsumption.objects.get(reservation=family_reservation).pk,
            TicketConsumption.objects.get(reservation=fixed_reservation).pk,
        )

    def test_stale_instances_cannot_double_consume_or_double_refund(self):
        self.purchase()
        reservation = self.reservation()
        stale = Reservation.objects.get(pk=reservation.pk)
        reservation.consume_tickets()
        self.assertIsNone(stale.consume_tickets())
        reservation.cancel()
        self.assertFalse(stale.cancel())
        self.assertEqual(TicketConsumption.objects.count(), 1)
        self.assertEqual(TicketLedger.objects.filter(reason=TicketLedger.REASON_RESERVATION_USE).count(), 1)
        self.assertEqual(TicketLedger.objects.filter(reason=TicketLedger.REASON_CANCEL_REFUND).count(), 1)

    def test_purchase_rolls_back_when_purchase_creation_fails(self):
        with patch.object(TicketPurchase.objects, "create", side_effect=RuntimeError("purchase failed")):
            with self.assertRaises(RuntimeError):
                self.purchase()
        self.member.refresh_from_db()
        self.assertEqual(self.member.ticket_balance, 0)
        self.assertFalse(TicketLedger.objects.exists())

    def test_consume_and_refund_roll_back_on_ledger_failure(self):
        self.purchase()
        reservation = self.reservation()
        with patch.object(TicketLedger.objects, "create", side_effect=RuntimeError("ledger failed")):
            with self.assertRaises(RuntimeError):
                reservation.consume_tickets()
        reservation.refresh_from_db()
        lot = TicketPurchase.objects.get()
        self.assertEqual(lot.remaining_tickets, 4)
        self.assertFalse(TicketConsumption.objects.exists())
        self.assertIsNone(reservation.ticket_consumed_at)

        reservation.consume_tickets()
        with patch.object(TicketLedger.objects, "create", side_effect=RuntimeError("refund failed")):
            with self.assertRaises(RuntimeError):
                reservation.cancel()
        reservation.refresh_from_db()
        lot.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.STATUS_ACTIVE)
        self.assertEqual(lot.remaining_tickets, 3)
        self.assertIsNone(TicketConsumption.objects.get().refunded_at)
