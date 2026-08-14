import json
from datetime import date
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from club.models import CoachExpense
from club.settlement_carry_integrity_diagnostic import (
    diagnose_settlement_carry_integrity,
)
from club.settlement_models import (
    CoachMonthlySettlement,
    MonthlySettlement,
    SettlementPayment,
)


class SettlementCarryIntegrityDiagnosticTests(TestCase):
    def setUp(self):
        self.coach = get_user_model().objects.create_user(
            "audit_coach", password="password12345", role="coach"
        )

    def settlement(self, year, month, **values):
        defaults = {"status": MonthlySettlement.STATUS_CLOSED}
        defaults.update(values)
        return MonthlySettlement.objects.create(year=year, month=month, **defaults)

    def coach_row(self, settlement, **values):
        defaults = {"coach": self.coach, "calculation_snapshot": {}}
        defaults.update(values)
        return CoachMonthlySettlement.objects.create(
            monthly_settlement=settlement, **defaults
        )

    def payment(self, settlement, **values):
        defaults = {
            "coach": self.coach, "payment_type": SettlementPayment.PAYMENT_TYPE_SALARY,
            "amount": 100, "paid_date": date(2026, 1, 31), "note": "private note",
        }
        defaults.update(values)
        payment = SettlementPayment(monthly_settlement=settlement, **defaults)
        SettlementPayment.objects.bulk_create([payment])
        return payment

    def test_active_duplicate_uses_service_key_and_excludes_reversed(self):
        settlement = self.settlement(2026, 1, salary_cash_out=200)
        self.coach_row(settlement)
        first = self.payment(settlement)
        second = self.payment(settlement)
        self.payment(settlement, is_reversed=True)
        result = diagnose_settlement_carry_integrity()
        self.assertEqual(result["duplicate_payments"], [{
            "monthly_settlement_id": settlement.pk, "year": 2026, "month": 1,
            "coach_id": self.coach.pk, "payment_type": "salary", "amount": 100,
            "paid_date": "2026-01-31", "active_payment_ids": [first.pk, second.pk],
            "count": 2,
        }])
        self.assertNotIn("private note", json.dumps(result))

    def test_normal_payment_and_duplicate_legacy_link(self):
        settlement = self.settlement(2026, 1, salary_cash_out=200)
        self.coach_row(settlement)
        one = self.payment(settlement, amount=75, legacy_coach_expense_id=9)
        two = self.payment(settlement, amount=125, legacy_coach_expense_id=9)
        result = diagnose_settlement_carry_integrity()
        self.assertEqual(result["duplicate_payments"], [])
        self.assertEqual(result["duplicate_legacy_links"][0]["payment_ids"], [one.pk, two.pk])

    def test_opening_chain_distinguishes_absent_open_and_closed_previous(self):
        january = self.settlement(2026, 1, closing_balance=500)
        february = self.settlement(2026, 2, status="draft", opening_balance=0)
        march = self.settlement(2026, 3, opening_balance=999)
        result = diagnose_settlement_carry_integrity()
        checks = {row["settlement_id"]: row for row in result["opening_balance_checks"]}
        self.assertIsNone(checks[january.pk]["matches_previous_closing"])
        self.assertFalse(checks[february.pk]["is_finding"])
        self.assertEqual(checks[march.pk]["previous_status"], "draft")
        self.assertFalse(checks[march.pk]["is_finding"])

    def test_closed_opening_mismatch_is_finding_and_year_boundary_matches(self):
        december = self.settlement(2025, 12, closing_balance=500)
        january = self.settlement(2026, 1, opening_balance=500, closing_balance=700)
        february = self.settlement(2026, 2, opening_balance=1)
        result = diagnose_settlement_carry_integrity()
        checks = {row["settlement_id"]: row for row in result["opening_balance_checks"]}
        self.assertTrue(checks[january.pk]["matches_previous_closing"])
        self.assertFalse(checks[january.pk]["is_finding"])
        self.assertTrue(checks[february.pk]["is_finding"])
        self.assertEqual(checks[january.pk]["previous_settlement_id"], december.pk)

    def test_missing_month_is_information_unless_target_has_carry(self):
        january = self.settlement(2026, 1)
        march = self.settlement(2026, 3)
        self.coach_row(january, salary_unpaid=200, calculation_snapshot={"negative_carry": 0})
        self.coach_row(march)
        case = diagnose_settlement_carry_integrity()["missing_month_carry_cases"][0]
        self.assertEqual(case["missing_months"], [{"year": 2026, "month": 2}])
        self.assertFalse(case["is_finding"])

    def test_missing_month_nonzero_target_carry_is_finding_across_year(self):
        november = self.settlement(2025, 11)
        january = self.settlement(2026, 1)
        self.coach_row(november)
        self.coach_row(january, calculation_snapshot={"negative_carry_in": 50})
        case = diagnose_settlement_carry_integrity()["missing_month_carry_cases"][0]
        self.assertEqual(case["missing_months"], [{"year": 2025, "month": 12}])
        self.assertTrue(case["is_finding"])

    def test_reimbursement_hypothesis_and_salary_coexistence(self):
        settlement = self.settlement(
            2026, 1, salary_cash_out=100, reimbursement_cash_out=200
        )
        self.coach_row(
            settlement, salary_paid=100, reimbursement_paid=200, salary_unpaid=700,
            calculation_snapshot={"wallet_final_entitlement": 1000},
        )
        self.payment(settlement, amount=100)
        self.payment(
            settlement, amount=200,
            payment_type=SettlementPayment.PAYMENT_TYPE_REIMBURSEMENT,
            legacy_coach_expense_id=44,
        )
        result = diagnose_settlement_carry_integrity()
        impact = result["legacy_reimbursement_impacts"][0]
        self.assertEqual(impact["hypothetical_salary_unpaid_without_reimbursement_payment"], 900)
        self.assertEqual(impact["difference"], 200)
        self.assertTrue(impact["salary_payment_coexists"])
        self.assertEqual(result["payment_summary"]["reimbursement_active"], 1)

    def test_no_reimbursement_has_no_impact_rows(self):
        settlement = self.settlement(2026, 1)
        self.coach_row(settlement)
        with self.assertNumQueries(3):
            result = diagnose_settlement_carry_integrity()
        self.assertEqual(result["legacy_reimbursement_impacts"], [])

    def test_double_carry_only_when_both_are_positive(self):
        january = self.settlement(2026, 1, unpaid_salary_total=100)
        self.coach_row(january, salary_unpaid=100, calculation_snapshot={"negative_carry": 0})
        february = self.settlement(2026, 2, opening_balance=0, unpaid_salary_total=20)
        self.coach_row(february, salary_unpaid=20, calculation_snapshot={"negative_carry": 30})
        result = diagnose_settlement_carry_integrity()
        self.assertEqual(len(result["double_carry_findings"]), 1)
        self.assertEqual(result["double_carry_findings"][0]["snapshot_key"], "negative_carry")

    def test_saved_totals_and_formal_active_payment_totals(self):
        settlement = self.settlement(
            2026, 1, unpaid_salary_total=50, unpaid_reimbursement_total=25,
            salary_cash_out=100, reimbursement_cash_out=40,
        )
        self.coach_row(settlement, salary_unpaid=50, reimbursement_unpaid=25)
        self.payment(settlement, amount=100)
        self.payment(settlement, amount=40, payment_type="reimbursement")
        self.assertEqual(diagnose_settlement_carry_integrity()["total_mismatches"], [])
        MonthlySettlement.objects.filter(pk=settlement.pk).update(
            unpaid_salary_total=51, reimbursement_cash_out=41
        )
        fields = {
            row["field"] for row in diagnose_settlement_carry_integrity()["total_mismatches"]
        }
        self.assertEqual(fields, {"unpaid_salary_total", "reimbursement_cash_out"})

    def test_reopen_chain_without_next_is_indeterminate(self):
        settlement = self.settlement(2026, 1, reopened_at=timezone.now())
        check = diagnose_settlement_carry_integrity()["reopen_chain_checks"][0]
        self.assertIsNone(check["next_settlement_id"])
        self.assertEqual(check["judgement"], "indeterminate")

    def test_reopened_and_reclosed_next_carry_match_and_mismatch(self):
        january = self.settlement(2026, 1, reopened_at=timezone.now())
        self.coach_row(january, salary_unpaid=20, calculation_snapshot={"negative_carry": 30})
        february = self.settlement(2026, 2)
        row = self.coach_row(
            february,
            calculation_snapshot={"negative_carry_in": 30, "unpaid_salary_carry_in": 20},
        )
        self.assertFalse(
            diagnose_settlement_carry_integrity()["reopen_chain_checks"][0]["possible_stale_carry"]
        )
        CoachMonthlySettlement.objects.filter(pk=row.pk).update(
            calculation_snapshot={"negative_carry_in": 1, "unpaid_salary_carry_in": 20}
        )
        check = diagnose_settlement_carry_integrity()["reopen_chain_checks"][0]
        self.assertTrue(check["possible_stale_carry"])
        self.assertEqual(check["judgement"], "mismatch")

    def test_reopened_draft_requires_zero_next_carry(self):
        january = self.settlement(2026, 1, status="draft", reopened_at=timezone.now())
        self.coach_row(january, salary_unpaid=20)
        february = self.settlement(2026, 2)
        self.coach_row(february, calculation_snapshot={"unpaid_salary_carry_in": 20})
        self.assertTrue(
            diagnose_settlement_carry_integrity()["reopen_chain_checks"][0]["possible_stale_carry"]
        )

    def test_command_is_deterministic_json_pii_free_and_database_unchanged(self):
        settlement = self.settlement(2026, 1)
        self.coach_row(settlement, calculation_snapshot={"sentinel": "unchanged"})
        self.payment(settlement, note="secret-personal-text")
        CoachExpense.objects.bulk_create([CoachExpense(
            expense_date=date(2026, 1, 1), amount=321,
            note="secret-expense-text", created_by=self.coach,
        )])
        models = (MonthlySettlement, CoachMonthlySettlement, SettlementPayment, CoachExpense)
        before = {
            model._meta.label: list(model.objects.order_by("pk").values()) for model in models
        }
        outputs = []
        for _ in range(2):
            stream = StringIO()
            call_command("diagnose_settlement_carry_integrity", stdout=stream)
            outputs.append(stream.getvalue().strip())
        self.assertEqual(outputs[0], outputs[1])
        parsed = json.loads(outputs[0])
        self.assertIn("finding_count", parsed)
        self.assertNotIn("secret-personal-text", outputs[0])
        self.assertNotIn("secret-expense-text", outputs[0])
        after = {
            model._meta.label: list(model.objects.order_by("pk").values()) for model in models
        }
        self.assertEqual(before, after)
