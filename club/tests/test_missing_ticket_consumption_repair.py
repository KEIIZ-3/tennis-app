from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from club.missing_ticket_consumption_repair import (
    inspect_missing_ticket_consumptions,
    repair_missing_ticket_consumptions,
)
from club.models import (
    CoachAvailability,
    Court,
    Reservation,
    TicketConsumption,
    TicketLedger,
    TicketPurchase,
    purchase_tickets,
)


class MissingTicketConsumptionRepairTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.member = users.objects.create_user(username="fifo-repair", full_name="FIFO Repair")
        self.coach = users.objects.create_user(username="fifo-coach", role=users.ROLE_COACH)
        self.court = Court.objects.create(name="FIFO repair court")
        self.base = timezone.make_aware(datetime.combine(timezone.localdate(), time(10)))

    def reservation(self, offset):
        start = self.base + timedelta(days=offset)
        availability = CoachAvailability.objects.create(
            coach=self.coach, court=self.court, start_at=start,
            end_at=start + timedelta(hours=2), capacity=20,
        )
        row = Reservation.objects.create(
            user=self.member, coach=self.coach, court=self.court,
            availability=availability, start_at=start,
            end_at=start + timedelta(hours=2), tickets_used=1,
        )
        row.consume_tickets()
        return row

    def purchase(self, tickets, price):
        return purchase_tickets(
            user=self.member, tickets=tickets, unit_price=price,
            purchase_type=TicketPurchase.PURCHASE_TYPE_SET4 if tickets == 4 else TicketPurchase.PURCHASE_TYPE_SINGLE,
            reason=TicketLedger.REASON_PURCHASE_SET4 if tickets == 4 else TicketLedger.REASON_PURCHASE_SINGLE,
            purchased_at=timezone.now() + timedelta(minutes=1),
        )[1]

    def simulate_missing_oldest(self, first, second, tickets, price):
        TicketConsumption.objects.get(reservation=first).delete()
        purchase = self.purchase(tickets, price)
        self.assertEqual(TicketConsumption.objects.get(reservation=second).purchase_id, purchase.pk)
        return purchase

    def test_non_positive_balance_always_has_pending_consumption(self):
        reservation = self.reservation(0)
        self.member.refresh_from_db()
        consumption = TicketConsumption.objects.get(reservation=reservation)
        self.assertEqual(self.member.ticket_balance, -1)
        self.assertIsNone(consumption.purchase_id)
        self.assertEqual(
            TicketLedger.objects.get(reservation=reservation).change_amount, -1
        )

    def test_one_ticket_purchase_allocates_oldest_pending(self):
        first = self.reservation(0)
        second = self.reservation(7)
        purchase = self.purchase(1, 4000)
        self.assertEqual(TicketConsumption.objects.get(reservation=first).purchase_id, purchase.pk)
        self.assertIsNone(TicketConsumption.objects.get(reservation=second).purchase_id)

    def test_ueda_equivalent_repair_reassigns_future_consumption(self):
        first = self.reservation(0)
        second = self.reservation(7)
        purchase = self.simulate_missing_oldest(first, second, 1, 4000)
        ledger_before = list(TicketLedger.objects.values_list("id", "change_amount"))

        preview = inspect_missing_ticket_consumptions(user_ids=[self.member.pk])
        self.assertEqual((preview[0].reservation_id, preview[0].expected_purchase_id), (first.pk, purchase.pk))
        repair_missing_ticket_consumptions(user_id=self.member.pk)

        purchase.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        self.member.refresh_from_db()
        self.assertEqual(TicketConsumption.objects.get(reservation=first).purchase_id, purchase.pk)
        self.assertIsNone(TicketConsumption.objects.get(reservation=second).purchase_id)
        self.assertIsNone(second.participant_ticket_price_snapshot)
        self.assertEqual((purchase.remaining_tickets, self.member.ticket_balance), (0, -1))
        self.assertEqual(list(TicketLedger.objects.values_list("id", "change_amount")), ledger_before)

    def test_shinohara_equivalent_repair_and_idempotency(self):
        first = self.reservation(0)
        second = self.reservation(7)
        purchase = self.simulate_missing_oldest(first, second, 4, 3500)
        repair_missing_ticket_consumptions(user_id=self.member.pk)
        repair_missing_ticket_consumptions(user_id=self.member.pk)

        purchase.refresh_from_db()
        self.member.refresh_from_db()
        self.assertEqual(
            list(TicketConsumption.objects.order_by("reservation__ticket_consumed_at").values_list("purchase_id", "unit_price_snapshot")),
            [(purchase.pk, 3500), (purchase.pk, 3500)],
        )
        self.assertEqual((purchase.remaining_tickets, self.member.ticket_balance), (2, 2))

    def test_preview_command_is_read_only(self):
        first = self.reservation(0)
        second = self.reservation(7)
        self.simulate_missing_oldest(first, second, 1, 4000)
        before = (TicketConsumption.objects.count(), list(TicketPurchase.objects.values_list("remaining_tickets", flat=True)))
        call_command("repair_missing_ticket_consumptions", user_id=[self.member.pk])
        self.assertEqual(before, (TicketConsumption.objects.count(), list(TicketPurchase.objects.values_list("remaining_tickets", flat=True))))

    def test_canceled_and_refunded_rows_are_not_candidates(self):
        canceled = self.reservation(0)
        canceled.cancel()
        active = self.reservation(7)
        consumption = TicketConsumption.objects.get(reservation=active)
        consumption.refunded_at = timezone.now()
        consumption.save(update_fields=["refunded_at"])
        self.assertEqual(inspect_missing_ticket_consumptions(user_ids=[self.member.pk]), [])
