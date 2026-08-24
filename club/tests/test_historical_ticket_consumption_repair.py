from datetime import datetime, time, timedelta
import json
from io import StringIO
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db.models import Sum
from django.db.models.query import QuerySet
from django.test import TestCase
from django.utils import timezone

from club.historical_ticket_consumption_repair import (
    inspect_historical_ticket_consumption_repair,
    repair_historical_ticket_consumption,
)
from club.models import Court, Reservation, TicketConsumption, TicketLedger, TicketPurchase, User
from club.participant_price_snapshot import is_ball_expense_eligible
from club.settlement_models import MonthlySettlement


class HistoricalTicketConsumptionRepairTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="historical", full_name="履歴 会員", ticket_balance=-1)
        self.coach = User.objects.create_user(username="coach", role=User.ROLE_COACH)
        self.court = Court.objects.create(name="historical court")
        self.start = timezone.make_aware(datetime.combine(timezone.localdate(), time(10)))

    def make_reservation(self, *, reservation_id=None, snapshot=None, ledger_count=1, consumed=True):
        row = Reservation(
            id=reservation_id, user=self.user, coach=self.coach, court=self.court,
            start_at=self.start, end_at=self.start + timedelta(hours=2), tickets_used=1,
            ticket_consumed_at=self.start if consumed else None,
            participant_ticket_price_snapshot=snapshot,
        )
        Reservation.objects.bulk_create([row])
        for _ in range(ledger_count):
            TicketLedger.objects.create(
                user=self.user, reservation=row, change_amount=-1, balance_after=-1,
                reason=TicketLedger.REASON_RESERVATION_USE,
            )
        return row

    def make_purchase(self, *, price=3500, remaining=2):
        return TicketPurchase.objects.create(
            user=self.user, total_tickets=remaining, remaining_tickets=remaining,
            unit_price=price, purchased_at=self.start + timedelta(days=1),
        )

    def test_purchase_linkage_changes_only_capacity_consumption_and_snapshot(self):
        reservation = self.make_reservation()
        purchase = self.make_purchase()
        settlement = MonthlySettlement.objects.create(
            year=self.start.year, month=self.start.month, cash_in_total=9000,
            cash_out_total=1000, closing_balance=8000,
            calculation_snapshot={"court_cost_total": 2400},
        )
        ledger_before = list(TicketLedger.objects.values_list("id", "change_amount"))
        court_before = reservation.court_id
        result = repair_historical_ticket_consumption(
            reservation.id, candidate_purchase_id=purchase.id
        )
        self.assertEqual(result.status, "repaired")
        consumption = TicketConsumption.objects.get(reservation=reservation)
        self.assertEqual((consumption.purchase_id, consumption.tickets_used, consumption.unit_price_snapshot), (purchase.id, 1, 3500))
        self.user.refresh_from_db(); purchase.refresh_from_db(); reservation.refresh_from_db(); settlement.refresh_from_db()
        self.assertEqual(self.user.ticket_balance, -1)
        self.assertEqual(purchase.remaining_tickets, 1)
        self.assertEqual(reservation.participant_ticket_price_snapshot, 3500)
        self.assertEqual(reservation.court_id, court_before)
        self.assertEqual(list(TicketLedger.objects.values_list("id", "change_amount")), ledger_before)
        self.assertEqual((settlement.cash_in_total, settlement.cash_out_total, settlement.closing_balance), (9000, 1000, 8000))

    def test_apply_locks_reservation_without_nullable_user_join(self):
        reservation = self.make_reservation()
        purchase = self.make_purchase()
        locked_reservation_queries = []
        original_fetch_all = QuerySet._fetch_all

        def capture_fetch_all(queryset):
            if queryset.model is Reservation and queryset.query.select_for_update:
                locked_reservation_queries.append(queryset.query)
            return original_fetch_all(queryset)

        with patch("django.db.models.query.QuerySet._fetch_all", capture_fetch_all):
            result = repair_historical_ticket_consumption(
                reservation.id, candidate_purchase_id=purchase.id
            )

        self.assertEqual(result.status, "repaired")
        self.assertTrue(locked_reservation_queries)
        for query in locked_reservation_queries:
            self.assertNotIn("JOIN", str(query).upper())

    def test_confirmed_price_without_purchase_and_no_fake_purchase(self):
        reservation = self.make_reservation(reservation_id=1525)
        before = TicketPurchase.objects.count()
        result = repair_historical_ticket_consumption(
            reservation.id, confirmed_unit_price=3500
        )
        consumption = TicketConsumption.objects.get(reservation=reservation)
        self.assertEqual(result.repair_mode, "confirmed_price_without_purchase")
        self.assertEqual((consumption.purchase_id, consumption.unit_price_snapshot), (None, 3500))
        self.assertEqual(TicketPurchase.objects.count(), before)
        self.user.refresh_from_db(); reservation.refresh_from_db()
        self.assertEqual(self.user.ticket_balance, -1)
        self.assertEqual(reservation.participant_ticket_price_snapshot, 3500)

    def test_saved_prices_and_ball_threshold_are_preserved(self):
        for index, price in enumerate((500, 1000, 3500)):
            reservation = self.make_reservation()
            purchase = self.make_purchase(price=price, remaining=1)
            repair_historical_ticket_consumption(reservation.id, candidate_purchase_id=purchase.id)
            reservation.refresh_from_db()
            self.assertEqual(reservation.participant_ticket_price_snapshot, price)
            self.assertEqual(is_ball_expense_eligible(reservation), price > 1000)

    def test_capacity_missing_ledger_and_consumed_marker_are_rejected(self):
        no_capacity = self.make_reservation()
        purchase = self.make_purchase(remaining=0)
        self.assertEqual(inspect_historical_ticket_consumption_repair(
            no_capacity.id, candidate_purchase_id=purchase.id
        ).reason, "candidate_purchase_capacity_insufficient")
        no_ledger = self.make_reservation(ledger_count=0)
        self.assertEqual(inspect_historical_ticket_consumption_repair(
            no_ledger.id, candidate_purchase_id=self.make_purchase().id
        ).reason, "single_reservation_use_ledger_required")
        two_ledgers = self.make_reservation(ledger_count=2)
        self.assertEqual(inspect_historical_ticket_consumption_repair(
            two_ledgers.id, candidate_purchase_id=self.make_purchase().id
        ).reason, "single_reservation_use_ledger_required")
        not_consumed = self.make_reservation(consumed=False)
        self.assertEqual(inspect_historical_ticket_consumption_repair(
            not_consumed.id, candidate_purchase_id=self.make_purchase().id
        ).reason, "ticket_consumed_at_missing")

    def test_existing_consumption_is_idempotent_noop(self):
        reservation = self.make_reservation()
        purchase = self.make_purchase()
        repair_historical_ticket_consumption(reservation.id, candidate_purchase_id=purchase.id)
        result = repair_historical_ticket_consumption(reservation.id, candidate_purchase_id=None)
        purchase.refresh_from_db()
        self.assertEqual((result.status, purchase.remaining_tickets), ("noop", 1))
        self.assertEqual(TicketConsumption.objects.filter(reservation=reservation).count(), 1)

    def test_restores_pending_evidence_without_changing_accounting_and_second_run_is_noop(self):
        reservation = self.make_reservation(reservation_id=1534)
        ledger_before = list(TicketLedger.objects.values())

        result = repair_historical_ticket_consumption(reservation.id)
        consumption = TicketConsumption.objects.get(reservation=reservation)
        self.assertEqual(result.repair_mode, "pending_purchase_evidence")
        self.assertEqual((consumption.purchase_id, consumption.unit_price_snapshot), (None, None))

        second = repair_historical_ticket_consumption(reservation.id)
        self.user.refresh_from_db(); reservation.refresh_from_db()
        self.assertEqual(second.status, "noop")
        self.assertEqual(self.user.ticket_balance, -1)
        self.assertEqual(reservation.tickets_used, 1)
        self.assertIsNone(reservation.participant_ticket_price_snapshot)
        self.assertEqual(list(TicketLedger.objects.values()), ledger_before)
        self.assertEqual(TicketConsumption.objects.filter(reservation=reservation).count(), 1)

    def test_pending_evidence_rejects_refunded_canceled_and_wrong_ledger_amount(self):
        refunded = self.make_reservation()
        Reservation.objects.filter(pk=refunded.pk).update(ticket_refunded_at=self.start)
        self.assertEqual(inspect_historical_ticket_consumption_repair(refunded.id).reason, "usage_refunded")
        canceled = self.make_reservation()
        Reservation.objects.filter(pk=canceled.pk).update(status=Reservation.STATUS_CANCELED)
        self.assertEqual(inspect_historical_ticket_consumption_repair(canceled.id).reason, "reservation_not_active")
        wrong_amount = self.make_reservation()
        TicketLedger.objects.filter(reservation=wrong_amount).update(change_amount=-2)
        self.assertEqual(inspect_historical_ticket_consumption_repair(wrong_amount.id).reason, "reservation_use_ledger_amount_mismatch")

    def test_pending_evidence_command_accepts_explicit_unapproved_id_and_defaults_to_dry_run(self):
        reservation = self.make_reservation(reservation_id=1534)
        stdout = StringIO()
        call_command(
            "repair_historical_ticket_consumptions",
            "--pending-evidence", "--reservation-id", str(reservation.id),
            stdout=stdout,
        )
        row = json.loads(stdout.getvalue())["rows"][0]
        self.assertEqual(row["repair_mode"], "pending_purchase_evidence")
        self.assertEqual(row["status"], "candidate")
        self.assertFalse(TicketConsumption.objects.filter(reservation=reservation).exists())

    def test_closed_month_is_rejected(self):
        reservation = self.make_reservation()
        purchase = self.make_purchase()
        MonthlySettlement.objects.create(
            year=self.start.year, month=self.start.month,
            status=MonthlySettlement.STATUS_CLOSED,
        )
        self.assertEqual(inspect_historical_ticket_consumption_repair(
            reservation.id, candidate_purchase_id=purchase.id
        ).reason, "accounting_month_closed")

    def test_failure_rolls_back_capacity_consumption_and_snapshot(self):
        reservation = self.make_reservation()
        purchase = self.make_purchase()
        with patch(
            "club.historical_ticket_consumption_repair._ledger_state",
            side_effect=[(1, -1), (1, -1), (2, -2)],
        ):
            with self.assertRaises(ValidationError):
                repair_historical_ticket_consumption(reservation.id, candidate_purchase_id=purchase.id)
        purchase.refresh_from_db(); reservation.refresh_from_db(); self.user.refresh_from_db()
        self.assertEqual(purchase.remaining_tickets, 2)
        self.assertIsNone(reservation.participant_ticket_price_snapshot)
        self.assertFalse(TicketConsumption.objects.filter(reservation=reservation).exists())
        self.assertEqual(TicketLedger.objects.aggregate(total=Sum("change_amount"))["total"], -1)

    def test_command_is_dry_run_by_default_and_reports_safety_fields(self):
        reservation = self.make_reservation(reservation_id=1505)
        purchase = TicketPurchase.objects.create(
            id=13, user=self.user, total_tickets=1, remaining_tickets=1,
            unit_price=500, purchased_at=self.start + timedelta(days=1),
        )
        audit_result = {"rows": [{
            "reservation_id": 1505,
            "candidate_later_purchase_id": 13,
            "candidate_purchase_unit_price": 500,
        }]}
        stdout = StringIO()
        with patch(
            "club.management.commands.repair_historical_ticket_consumptions.audit_missing_ticket_purchase_evidence",
            return_value=audit_result,
        ):
            call_command(
                "repair_historical_ticket_consumptions", "--reservation-id", "1505",
                stdout=stdout,
            )
        row = json.loads(stdout.getvalue())["rows"][0]
        required = {
            "reservation_id", "participant_name", "ticket_balance_before",
            "ticket_balance_after_expected", "ledger_count_before", "ledger_delta_before",
            "ledger_count_after_expected", "ledger_delta_after_expected",
            "existing_consumption_count", "candidate_purchase_id",
            "candidate_purchase_unit_price", "repair_mode", "purchase_remaining_before",
            "purchase_remaining_after_expected", "participant_ticket_price_before",
            "participant_ticket_price_after_expected", "will_change_balance",
            "will_create_ledger", "will_change_wallet", "will_change_court_settlement",
            "candidate", "reason",
        }
        self.assertTrue(required.issubset(row))
        self.assertEqual((row["candidate_purchase_id"], row["candidate_purchase_unit_price"]), (13, 500))
        self.assertFalse(TicketConsumption.objects.filter(reservation=reservation).exists())
        purchase.refresh_from_db()
        self.assertEqual(purchase.remaining_tickets, 1)
