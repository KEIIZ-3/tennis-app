from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from club.deferred_ticket_consumption import allocate_pending_ticket_consumptions
from club.models import (
    CoachAvailability, Court, Reservation, ReservationParticipant, TicketConsumption, TicketLedger,
    TicketPurchase, purchase_tickets,
)


class DeferredTicketConsumptionTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.user = users.objects.create_user(username="deferred-owner")
        self.coach = users.objects.create_user(username="deferred-coach", role=users.ROLE_COACH)
        self.court = Court.objects.create(name="Deferred court")
        self.base = timezone.make_aware(datetime.combine(timezone.localdate() + timedelta(days=2), time(9)))

    def consume(self, *, user=None, offset=0, tickets=1):
        owner = user or self.user
        start = self.base + timedelta(days=offset)
        availability = CoachAvailability.objects.create(
            coach=self.coach, court=self.court, start_at=start,
            end_at=start + timedelta(hours=2), capacity=20,
        )
        reservation = Reservation.objects.create(
            user=owner, coach=self.coach, court=self.court, availability=availability,
            start_at=start, end_at=start + timedelta(hours=2), tickets_used=tickets,
        )
        reservation.consume_tickets()
        return reservation

    def purchase(self, *, tickets=4, price=3500, purchase_type=TicketPurchase.PURCHASE_TYPE_SET4):
        return purchase_tickets(
            user=self.user, tickets=tickets, unit_price=price,
            purchase_type=purchase_type, reason=TicketLedger.REASON_PURCHASE_SET4,
        )[1]

    def test_zero_balance_creates_pending_then_purchase_links_without_second_ledger(self):
        reservation = self.consume()
        pending = TicketConsumption.objects.get(reservation=reservation)
        self.assertEqual((self.user.ticket_balance, pending.purchase_id, pending.unit_price_snapshot), (-1, None, None))
        before_use_ledgers = TicketLedger.objects.filter(change_amount=-1).count()

        lot = self.purchase()
        self.user.refresh_from_db(); lot.refresh_from_db(); reservation.refresh_from_db(); pending.refresh_from_db()
        self.assertEqual((self.user.ticket_balance, lot.remaining_tickets), (3, 3))
        self.assertEqual((pending.purchase_id, pending.unit_price_snapshot), (lot.id, 3500))
        self.assertEqual(reservation.participant_ticket_price_snapshot, 3500)
        self.assertEqual(TicketLedger.objects.filter(change_amount=-1).count(), before_use_ledgers)
        self.assertEqual(TicketLedger.objects.count(), 2)

    def test_fifo_capacity_three_five_and_multiple_purchases(self):
        # Existing policy permits a minimum balance of -4, so one legacy
        # balance unit lets this fixture represent five unsettled usages.
        self.user.ticket_balance = 1
        self.user.save(update_fields=["ticket_balance"])
        reservations = [self.consume(offset=index) for index in range(5)]
        first = self.purchase()
        self.user.refresh_from_db(); first.refresh_from_db()
        self.assertEqual((self.user.ticket_balance, first.remaining_tickets), (0, 0))
        self.assertEqual(
            list(TicketConsumption.objects.filter(purchase=first).values_list("reservation_id", flat=True)),
            [row.id for row in reservations[:4]],
        )
        self.assertTrue(TicketConsumption.objects.filter(reservation=reservations[4], purchase__isnull=True).exists())
        second = self.purchase(tickets=1, price=4000, purchase_type=TicketPurchase.PURCHASE_TYPE_SINGLE)
        self.assertEqual(TicketConsumption.objects.get(reservation=reservations[4]).purchase_id, second.id)

    def test_three_pending_leave_one_available(self):
        for index in range(3):
            self.consume(offset=index)
        lot = self.purchase()
        self.user.refresh_from_db(); lot.refresh_from_db()
        self.assertEqual((self.user.ticket_balance, lot.remaining_tickets), (1, 1))

    def test_family_participants_share_user_fifo_and_keep_snapshots(self):
        first = self.consume()
        second = self.consume(offset=1)
        ReservationParticipant.objects.create(
            reservation=first, parent=self.user, participant_type="family",
            participant_name="Family A",
        )
        ReservationParticipant.objects.create(
            reservation=second, parent=self.user, participant_type="family",
            participant_name="Family B",
        )
        lot = self.purchase(tickets=2)
        linked = list(TicketConsumption.objects.filter(purchase=lot).order_by("id"))
        self.assertEqual([row.reservation_id for row in linked], [first.id, second.id])
        self.assertEqual(
            list(ReservationParticipant.objects.order_by("reservation_id").values_list("participant_name", flat=True)),
            ["Family A", "Family B"],
        )

    def test_saved_admin_and_threshold_prices_are_not_repriced(self):
        for price in (500, 1000, 1001, 0):
            reservation = self.consume(offset=len(Reservation.objects.all()) + 1)
            kind = TicketPurchase.PURCHASE_TYPE_FORMAL_FREE if price == 0 else TicketPurchase.PURCHASE_TYPE_ADMIN
            lot = self.purchase(tickets=1, price=price, purchase_type=kind)
            reservation.refresh_from_db()
            self.assertEqual(TicketConsumption.objects.get(reservation=reservation).unit_price_snapshot, price)
            self.assertEqual(reservation.participant_ticket_price_snapshot, price)

        from club.participant_price_snapshot import is_ball_expense_eligible
        rows = list(Reservation.objects.order_by("id"))
        self.assertEqual([is_ball_expense_eligible(row) for row in rows], [False, False, True, False])

    def test_canceled_and_refunded_pending_are_not_linked(self):
        canceled = self.consume()
        canceled.cancel()
        refunded_row = TicketConsumption.objects.get(reservation=canceled)
        lot = self.purchase(tickets=1)
        refunded_row.refresh_from_db(); lot.refresh_from_db()
        self.assertIsNone(refunded_row.purchase_id)
        self.assertIsNotNone(refunded_row.refunded_at)
        self.assertEqual(lot.remaining_tickets, 1)

    def test_purchase_earlier_than_consumption_does_not_link_and_duplicate_is_noop(self):
        reservation = self.consume()
        marker = reservation.ticket_consumed_at
        lot = TicketPurchase.objects.create(
            user=self.user, total_tickets=1, remaining_tickets=1, unit_price=3500,
            purchased_at=marker - timedelta(seconds=1),
        )
        self.assertEqual(allocate_pending_ticket_consumptions(lot), [])
        lot.purchased_at = marker + timedelta(seconds=1); lot.save(update_fields=["purchased_at"])
        allocate_pending_ticket_consumptions(lot)
        self.assertEqual(allocate_pending_ticket_consumptions(lot), [])
        self.assertEqual(TicketConsumption.objects.filter(reservation=reservation).count(), 1)

    def test_allocation_failure_rolls_back_purchase_and_balance(self):
        self.consume()
        self.user.refresh_from_db()
        with patch("club.deferred_ticket_consumption.allocate_pending_ticket_consumptions", side_effect=RuntimeError("link failed")):
            with self.assertRaises(RuntimeError):
                self.purchase()
        self.user.refresh_from_db()
        self.assertEqual(self.user.ticket_balance, -1)
        self.assertFalse(TicketPurchase.objects.exists())
        self.assertEqual(TicketLedger.objects.count(), 1)
