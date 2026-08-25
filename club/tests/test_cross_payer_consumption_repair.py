from datetime import datetime, time, timedelta
import json
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from club.cross_payer_consumption_repair import (
    inspect_cross_payer_consumption_repair,
    repair_cross_payer_consumption,
)
from club.models import (
    Court, Reservation, TicketBurdenChange, TicketConsumption,
    TicketLedger, TicketPurchase, User,
)
from club.settlement_models import MonthlySettlement


class CrossPayerConsumptionRepairTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", ticket_balance=5)
        self.payer = User.objects.create_user(username="payer", ticket_balance=7)
        self.coach = User.objects.create_user(username="coach", role=User.ROLE_COACH)
        self.court = Court.objects.create(name="court")
        self.start = timezone.make_aware(datetime.combine(timezone.localdate(), time(10)))
        self.reservation = Reservation(
            user=self.owner, coach=self.coach, court=self.court,
            start_at=self.start, end_at=self.start + timedelta(hours=2),
            tickets_used=1, ticket_consumed_at=self.start,
            participant_ticket_price_snapshot=0,
        )
        Reservation.objects.bulk_create([self.reservation])
        self.refunded = TicketConsumption.objects.create(
            user=self.owner, reservation=self.reservation, tickets_used=1,
            unit_price_snapshot=None, refunded_at=self.start,
        )
        self.pending = TicketConsumption.objects.create(
            user=self.payer, reservation=self.reservation, tickets_used=1,
            unit_price_snapshot=None,
        )
        self.change = TicketBurdenChange.objects.create(
            reservation=self.reservation, previous_payer=self.owner,
            new_payer=self.payer, tickets=1, created_by=self.coach,
        )
        self.purchase = TicketPurchase.objects.create(
            user=self.payer, total_tickets=4, remaining_tickets=3,
            unit_price=3500, purchased_at=self.start - timedelta(days=2),
        )
        TicketLedger.objects.create(
            user=self.owner, reservation=self.reservation, change_amount=-1,
            balance_after=4, reason=TicketLedger.REASON_RESERVATION_USE,
        )
        TicketLedger.objects.create(
            user=self.owner, reservation=self.reservation, change_amount=1,
            balance_after=5, reason=TicketLedger.REASON_CANCEL_REFUND,
        )
        TicketLedger.objects.create(
            user=self.payer, reservation=self.reservation, change_amount=-1,
            balance_after=7, reason=TicketLedger.REASON_RESERVATION_USE,
        )

    def test_repairs_existing_row_and_preserves_accounting_evidence(self):
        user_state = list(User.objects.order_by("id").values_list("id", "ticket_balance"))
        ledger_state = list(TicketLedger.objects.order_by("id").values())
        burden_state = list(TicketBurdenChange.objects.values())
        consumption_ids = list(TicketConsumption.objects.order_by("id").values_list("id", flat=True))

        result = repair_cross_payer_consumption(self.reservation.id)

        self.pending.refresh_from_db(); self.purchase.refresh_from_db(); self.reservation.refresh_from_db()
        self.assertEqual(result.status, "repaired")
        self.assertEqual((self.pending.purchase_id, self.pending.unit_price_snapshot), (self.purchase.id, 3500))
        self.assertEqual(self.purchase.remaining_tickets, 2)
        self.assertEqual(self.reservation.participant_ticket_price_snapshot, 3500)
        self.assertEqual(self.reservation.tickets_used, 1)
        self.assertEqual(list(User.objects.order_by("id").values_list("id", "ticket_balance")), user_state)
        self.assertEqual(list(TicketLedger.objects.order_by("id").values()), ledger_state)
        self.assertEqual(list(TicketBurdenChange.objects.values()), burden_state)
        self.assertEqual(list(TicketConsumption.objects.order_by("id").values_list("id", flat=True)), consumption_ids)

    def test_second_apply_is_noop(self):
        repair_cross_payer_consumption(self.reservation.id)
        result = repair_cross_payer_consumption(self.reservation.id)
        self.purchase.refresh_from_db()
        self.assertEqual((result.status, result.reason), ("noop", "consumption_already_priced"))
        self.assertEqual(self.purchase.remaining_tickets, 2)

    def test_preview_command_does_not_write(self):
        stdout = StringIO()
        call_command("repair_cross_payer_consumptions", "--reservation-id", str(self.reservation.id), stdout=stdout)
        row = json.loads(stdout.getvalue())
        self.assertTrue(row["dry_run"])
        self.assertEqual((row["rows"][0]["purchase_id"], row["rows"][0]["unit_price"]), (self.purchase.id, 3500))
        self.pending.refresh_from_db(); self.purchase.refresh_from_db(); self.reservation.refresh_from_db()
        self.assertIsNone(self.pending.purchase_id)
        self.assertEqual(self.purchase.remaining_tickets, 3)
        self.assertEqual(self.reservation.participant_ticket_price_snapshot, 0)

    def test_requires_matching_latest_formal_change(self):
        self.change.delete()
        self.assertEqual(inspect_cross_payer_consumption_repair(self.reservation.id).reason, "formal_burden_change_required")
        with self.assertRaises(CommandError):
            call_command("repair_cross_payer_consumptions", "--reservation-id", str(self.reservation.id), stdout=StringIO())

    def test_rejects_insufficient_capacity_without_guessing(self):
        self.purchase.remaining_tickets = 0
        self.purchase.save(update_fields=["remaining_tickets"])
        self.assertEqual(inspect_cross_payer_consumption_repair(self.reservation.id).reason, "fifo_purchase_capacity_insufficient")

    def test_uses_oldest_available_purchase(self):
        older = TicketPurchase.objects.create(
            user=self.payer, total_tickets=1, remaining_tickets=1,
            unit_price=3000, purchased_at=self.start - timedelta(days=3),
        )
        result = inspect_cross_payer_consumption_repair(self.reservation.id)
        self.assertEqual((result.purchase_id, result.unit_price), (older.id, 3000))

    def test_closed_month_is_rejected(self):
        MonthlySettlement.objects.create(
            year=self.start.year, month=self.start.month,
            status=MonthlySettlement.STATUS_CLOSED,
        )
        self.assertEqual(inspect_cross_payer_consumption_repair(self.reservation.id).reason, "accounting_month_closed")

    def test_zero_snapshot_is_not_enough_without_unpriced_consumption(self):
        self.pending.unit_price_snapshot = 3500
        self.pending.save(update_fields=["unit_price_snapshot"])
        result = inspect_cross_payer_consumption_repair(self.reservation.id)
        self.assertEqual((result.status, result.reason), ("noop", "consumption_already_priced"))
