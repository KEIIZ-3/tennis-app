from datetime import datetime, time, timedelta
from unittest.mock import patch
from io import StringIO
import json

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db.models import Sum
from django.test import TestCase
from django.utils import timezone

from club.legacy_ticket_consumption_repair import (
    RepairRejected,
    inspect_legacy_ticket_consumption_repair,
    repair_legacy_ticket_consumption,
)
from club.models import Court, Reservation, TicketConsumption, TicketLedger, TicketPurchase, User
from club.settlement_models import MonthlySettlement


class LegacyTicketConsumptionRepairTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="member", ticket_balance=3)
        self.coach = User.objects.create_user(username="coach", role=User.ROLE_COACH)
        self.court = Court.objects.create(name="court")
        now = timezone.make_aware(datetime.combine(timezone.localdate(), time(10)))
        self.purchase = TicketPurchase.objects.create(
            user=self.user, purchase_type=TicketPurchase.PURCHASE_TYPE_LEGACY,
            total_tickets=4, remaining_tickets=3, unit_price=3500,
            purchased_at=now - timedelta(days=2),
        )
        self.reservation = Reservation(
            user=self.user, coach=self.coach, court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE,
            start_at=now - timedelta(days=1), end_at=now - timedelta(days=1) + timedelta(hours=1),
            tickets_used=1, ticket_consumed_at=now - timedelta(days=1),
            participant_ticket_price_snapshot=3500,
        )
        Reservation.objects.bulk_create([self.reservation])
        self.ledger = TicketLedger.objects.create(
            user=self.user, reservation=self.reservation, change_amount=-1,
            balance_after=3, reason=TicketLedger.REASON_RESERVATION_USE,
        )

    def test_repair_only_creates_consumption_and_preserves_all_accounting_state(self):
        balance = self.user.ticket_balance
        remaining = self.purchase.remaining_tickets
        ledger_state = list(TicketLedger.objects.values_list("id", "change_amount"))
        reservation_state = (self.reservation.court_id, self.reservation.start_at, self.reservation.end_at)
        with patch.object(Reservation, "consume_tickets", side_effect=AssertionError("must not run")):
            result = repair_legacy_ticket_consumption(self.reservation.id)
        self.assertEqual(result.status, "repaired")
        consumption = TicketConsumption.objects.get(reservation=self.reservation)
        self.assertEqual((consumption.purchase_id, consumption.tickets_used, consumption.unit_price_snapshot), (self.purchase.id, 1, 3500))
        self.user.refresh_from_db(); self.purchase.refresh_from_db(); self.reservation.refresh_from_db()
        self.assertEqual(self.user.ticket_balance, balance)
        self.assertEqual(self.purchase.remaining_tickets, remaining)
        self.assertEqual(list(TicketLedger.objects.values_list("id", "change_amount")), ledger_state)
        self.assertEqual((self.reservation.court_id, self.reservation.start_at, self.reservation.end_at), reservation_state)

    def test_ambiguous_purchase_and_unknown_price_are_rejected(self):
        TicketPurchase.objects.create(user=self.user, total_tickets=1, remaining_tickets=0, unit_price=3500, purchased_at=self.purchase.purchased_at)
        self.assertEqual(inspect_legacy_ticket_consumption_repair(self.reservation.id).reason, "unique_purchase_lot_required")
        TicketPurchase.objects.exclude(pk=self.purchase.pk).delete()
        self.purchase.unit_price = 0; self.purchase.save(update_fields=["unit_price"])
        self.reservation.participant_ticket_price_snapshot = 0; self.reservation.save(update_fields=["participant_ticket_price_snapshot"])
        self.assertEqual(inspect_legacy_ticket_consumption_repair(self.reservation.id).reason, "unit_price_evidence_missing")

    def test_existing_consumption_is_idempotent_noop(self):
        repair_legacy_ticket_consumption(self.reservation.id)
        result = repair_legacy_ticket_consumption(self.reservation.id)
        self.assertEqual(result.status, "noop")
        self.assertEqual(TicketConsumption.objects.filter(reservation=self.reservation).count(), 1)

    def test_exception_rolls_back_completely(self):
        with patch("club.legacy_ticket_consumption_repair._assert_invariants", side_effect=RepairRejected("forced")):
            with self.assertRaises(ValidationError):
                repair_legacy_ticket_consumption(self.reservation.id)
        self.assertFalse(TicketConsumption.objects.filter(reservation=self.reservation).exists())
        self.user.refresh_from_db(); self.purchase.refresh_from_db()
        self.assertEqual(self.user.ticket_balance, 3)
        self.assertEqual(self.purchase.remaining_tickets, 3)
        self.assertEqual(TicketLedger.objects.aggregate(total=Sum("change_amount"))["total"], -1)

    def test_dry_run_reports_required_invariants_and_writes_nothing(self):
        stdout = StringIO()
        call_command("repair_legacy_ticket_consumptions", "--reservation-id", str(self.reservation.id), stdout=stdout)
        row = json.loads(stdout.getvalue())["rows"][0]
        required = {
            "reservation_id", "user_id", "participant_name", "ticket_balance_before",
            "ticket_balance_after_expected", "ledger_delta_before", "ledger_delta_after_expected",
            "candidate_purchase_id", "candidate_unit_price", "new_consumption_tickets",
            "new_consumption_value", "will_change_balance", "will_create_ledger",
            "will_change_wallet",
        }
        self.assertTrue(required.issubset(row))
        self.assertFalse(row["will_change_balance"])
        self.assertFalse(row["will_create_ledger"])
        self.assertFalse(TicketConsumption.objects.filter(reservation=self.reservation).exists())

    def test_wallet_cash_and_court_settlement_are_unchanged(self):
        settlement = MonthlySettlement.objects.create(
            year=self.reservation.start_at.year, month=self.reservation.start_at.month,
            cash_in_total=12000, cash_out_total=4000, closing_balance=8000,
            calculation_snapshot={"court_cost_total": 2500},
        )
        before = (settlement.cash_in_total, settlement.cash_out_total, settlement.closing_balance, settlement.calculation_snapshot)
        repair_legacy_ticket_consumption(self.reservation.id)
        settlement.refresh_from_db()
        self.assertEqual((settlement.cash_in_total, settlement.cash_out_total, settlement.closing_balance, settlement.calculation_snapshot), before)
