import json
from datetime import datetime, timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from club.court_expense_transfer import court_transfer_summary_for_availability
from club.expense_metadata import build_expense_note, parse_expense_note
from club.models import (
    MAIN_COACH_NAMES,
    CoachAvailability,
    CoachExpense,
    Court,
    Reservation,
    User,
)
from club.prepaid_court_transfer_repair import repair_prepaid_court_transfer
from club.settlement_models import MonthlySettlement


class PrepaidCourtTransferRepairTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(
            username="repair-owner",
            role=User.ROLE_COACH,
            full_name=MAIN_COACH_NAMES[0],
        )
        self.payer = User.objects.create_user(
            username="repair-payer", role=User.ROLE_COACH, full_name="Repair Payer"
        )
        self.court = Court.objects.create(
            name="Nishi-Ina Park", court_type=Court.COURT_SONO, is_active=True
        )
        self.start = timezone.make_aware(datetime(2026, 10, 11, 19, 0))

    def availability(self, **overrides):
        values = {
            "coach": self.coach,
            "court": self.court,
            "lesson_type": Reservation.LESSON_GENERAL,
            "start_at": self.start,
            "end_at": self.start + timedelta(hours=2),
            "capacity": 4,
            "court_count": 1,
            "court_payer_kind": "company_wallet",
        }
        values.update(overrides)
        return CoachAvailability.objects.create(**values)

    def run_command(self, *args):
        stdout = StringIO()
        call_command("repair_prepaid_court_transfers", *args, stdout=stdout)
        return json.loads(stdout.getvalue())

    def test_company_wallet_automatic_fee_creates_3200_transfer_and_updates_wallet(self):
        availability = self.availability()

        result = repair_prepaid_court_transfer(availability.pk)

        self.assertEqual(result.status, "created")
        expense = CoachExpense.objects.get()
        meta = parse_expense_note(expense.note)
        self.assertEqual(expense.amount, 3200)
        self.assertEqual(meta["payer_kind"], "company_wallet")
        self.assertEqual(meta["availability_id"], availability.pk)
        self.assertEqual(
            court_transfer_summary_for_availability(availability)["status"],
            "registered",
        )
        settlement = MonthlySettlement.objects.get(year=2026, month=10)
        self.assertEqual(
            settlement.calculation_snapshot["company_paid_court_total"], 3200
        )
        self.assertEqual(settlement.cash_out_total, 3200)
        self.assertEqual(settlement.closing_balance, -3200)

    def test_coach_payer_automatic_fee_is_created(self):
        availability = self.availability(
            court_payer_kind="coach", court_payer_coach=self.payer
        )
        repair_prepaid_court_transfer(availability.pk)
        meta = parse_expense_note(CoachExpense.objects.get().note)
        self.assertEqual(meta["payer_kind"], "coach")
        self.assertEqual(meta["payer_coach_id"], self.payer.pk)

    def test_manual_override_and_manual_zero_are_preserved(self):
        manual = self.availability(court_fee_overridden=True, court_fee_amount=900)
        zero = self.availability(
            start_at=self.start + timedelta(days=7),
            end_at=self.start + timedelta(days=7, hours=2),
            court_fee_overridden=True,
            court_fee_amount=0,
        )
        repair_prepaid_court_transfer(manual.pk)
        repair_prepaid_court_transfer(zero.pk)
        amounts = sorted(CoachExpense.objects.values_list("amount", flat=True))
        self.assertEqual(amounts, [0, 900])
        zero_meta = parse_expense_note(CoachExpense.objects.get(amount=0).note)
        self.assertFalse(zero_meta["court_cost_not_required"])
        self.assertEqual(zero_meta["payer_kind"], "company_wallet")

    def test_unknown_court_requires_manual_amount(self):
        other = Court.objects.create(
            name="Other court", court_type=Court.COURT_OTHER, is_active=True
        )
        manual = self.availability(
            court=other, court_fee_overridden=True, court_fee_amount=1200
        )
        unresolved = self.availability(
            court=other,
            start_at=self.start + timedelta(days=1),
            end_at=self.start + timedelta(days=1, hours=2),
        )
        self.assertEqual(repair_prepaid_court_transfer(manual.pk).status, "created")
        self.assertEqual(
            repair_prepaid_court_transfer(unresolved.pk).status,
            "skipped_unresolved_amount",
        )

    def test_missing_and_invalid_coach_payers_are_skipped(self):
        unset = self.availability(court_payer_kind="")
        invalid = self.availability(
            start_at=self.start + timedelta(days=1),
            end_at=self.start + timedelta(days=1, hours=2),
        )
        CoachAvailability.objects.filter(pk=invalid.pk).update(
            court_payer_kind="coach", court_payer_coach=None
        )
        invalid.refresh_from_db()
        self.assertEqual(
            repair_prepaid_court_transfer(unset.pk).status, "skipped_missing_payer"
        )
        self.assertEqual(
            repair_prepaid_court_transfer(invalid.pk).status, "skipped_missing_payer"
        )
        self.assertEqual(CoachExpense.objects.count(), 0)

    def test_existing_transfer_and_second_run_are_idempotent(self):
        availability = self.availability()
        first = repair_prepaid_court_transfer(availability.pk)
        second = repair_prepaid_court_transfer(availability.pk)
        self.assertEqual(first.status, "created")
        self.assertEqual(second.status, "skipped_existing")
        self.assertEqual(CoachExpense.objects.count(), 1)

    def test_preexisting_transfer_is_not_modified(self):
        availability = self.availability()
        expense = CoachExpense.objects.create(
            expense_date=self.start.date(),
            category=CoachExpense.CATEGORY_COURT,
            amount=777,
            created_by=self.coach,
            note=build_expense_note({
                "record_kind": "court_transfer",
                "availability_id": availability.pk,
            }),
        )
        self.assertEqual(
            repair_prepaid_court_transfer(availability.pk).status,
            "skipped_existing",
        )
        expense.refresh_from_db()
        self.assertEqual(expense.amount, 777)

    def test_closed_month_is_skipped_with_month_in_result(self):
        availability = self.availability()
        MonthlySettlement.objects.create(
            year=2026, month=10, status=MonthlySettlement.STATUS_CLOSED
        )
        result = repair_prepaid_court_transfer(availability.pk)
        self.assertEqual(result.status, "skipped_closed_month")
        self.assertEqual((result.year, result.month), (2026, 10))
        self.assertEqual(CoachExpense.objects.count(), 0)

    def test_dry_run_and_availability_id_filter_do_not_write(self):
        target = self.availability()
        self.availability(
            start_at=self.start + timedelta(days=1),
            end_at=self.start + timedelta(days=1, hours=2),
        )
        payload = self.run_command("--availability-id", str(target.pk))
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["target_count"], 1)
        self.assertEqual(payload["candidates"], 1)
        self.assertEqual(payload["rows"][0]["amount"], 3200)
        self.assertEqual(payload["rows"][0]["payer_kind"], "company_wallet")
        self.assertEqual(CoachExpense.objects.count(), 0)

    def test_apply_command_reports_required_counts(self):
        target = self.availability()
        payload = self.run_command(
            "--availability-id", str(target.pk), "--apply"
        )
        self.assertFalse(payload["dry_run"])
        self.assertEqual(payload["created"], 1)
        for key in (
            "skipped_existing",
            "skipped_closed_month",
            "skipped_missing_payer",
            "skipped_unresolved_amount",
            "errors",
        ):
            self.assertIn(key, payload)
