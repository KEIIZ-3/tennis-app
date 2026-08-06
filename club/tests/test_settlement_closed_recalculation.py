from datetime import date, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.settlement_models import (
    CoachMonthlySettlement,
    MonthlySettlement,
    SettlementPayment,
)
from club.settlement_service import calculate_monthly_settlement


MONTHLY_FIELDS = (
    "id", "year", "month", "status", "opening_balance", "cash_in_total",
    "cash_out_total", "closing_balance", "ticket_cash_in", "preopen_cash_in",
    "stringing_cash_in", "other_cash_in", "salary_cash_out",
    "reimbursement_cash_out", "common_expense_cash_out", "contractor_cash_out",
    "other_cash_out", "unpaid_salary_total", "unpaid_reimbursement_total",
    "uncollected_revenue_total", "calculation_snapshot", "note", "closed_at",
    "closed_by_id", "reopened_at", "reopened_by_id", "created_at", "updated_at",
)
COACH_FIELDS = (
    "id", "monthly_settlement_id", "coach_id", "is_contractor_coach",
    "lesson_count", "ticket_revenue", "preopen_paid_revenue",
    "preopen_unpaid_revenue", "stringing_revenue", "contractor_work_amount",
    "common_expense_share", "reimbursement_carry_in",
    "reimbursement_current_month", "reimbursement_due", "salary_due",
    "salary_paid", "salary_unpaid", "reimbursement_paid",
    "reimbursement_unpaid", "calculation_snapshot", "created_at", "updated_at",
)
PAYMENT_FIELDS = (
    "id", "monthly_settlement_id", "coach_id", "payment_type", "amount",
    "paid_date", "note", "legacy_coach_expense_id", "is_reversed", "reversed_at",
    "reversed_by_id", "reversal_note", "created_by_id", "created_at", "updated_at",
)


class ClosedSettlementRecalculationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            username="closed_settlement_admin",
            password="password12345",
            role=User.ROLE_COACH,
            is_staff=True,
        )
        self.coach = User.objects.create_user(
            username="closed_settlement_coach",
            password="password12345",
            role=User.ROLE_COACH,
        )
        fixed_time = timezone.make_aware(datetime(2026, 7, 31, 23, 0))
        self.settlement = MonthlySettlement.objects.create(
            year=2026,
            month=7,
            status=MonthlySettlement.STATUS_CLOSED,
            opening_balance=101,
            cash_in_total=202,
            cash_out_total=303,
            closing_balance=404,
            ticket_cash_in=505,
            preopen_cash_in=606,
            stringing_cash_in=707,
            other_cash_in=808,
            salary_cash_out=909,
            reimbursement_cash_out=1001,
            common_expense_cash_out=1102,
            contractor_cash_out=1203,
            other_cash_out=1304,
            unpaid_salary_total=1405,
            unpaid_reimbursement_total=1506,
            uncollected_revenue_total=1607,
            calculation_snapshot={"sentinel": "closed snapshot", "closing_balance": 404},
            note="closed note",
            closed_at=fixed_time,
            closed_by=self.admin,
            created_at=fixed_time,
            updated_at=fixed_time,
        )
        self.coach_row = CoachMonthlySettlement.objects.create(
            monthly_settlement=self.settlement,
            coach=self.coach,
            lesson_count=2,
            ticket_revenue=2000,
            preopen_paid_revenue=300,
            preopen_unpaid_revenue=400,
            stringing_revenue=500,
            contractor_work_amount=600,
            common_expense_share=700,
            reimbursement_carry_in=800,
            reimbursement_current_month=900,
            reimbursement_due=1000,
            salary_due=1100,
            salary_paid=1200,
            salary_unpaid=1300,
            reimbursement_paid=1400,
            reimbursement_unpaid=1500,
            calculation_snapshot={"sentinel": "coach snapshot"},
            created_at=fixed_time,
            updated_at=fixed_time,
        )
        SettlementPayment.objects.bulk_create([
            SettlementPayment(
                monthly_settlement=self.settlement,
                coach=self.coach,
                payment_type=SettlementPayment.PAYMENT_TYPE_SALARY,
                amount=123,
                paid_date=date(2026, 7, 31),
                note="closed payment",
                is_reversed=True,
                reversed_at=fixed_time,
                reversed_by=self.admin,
                reversal_note="saved reversal",
                created_by=self.admin,
                created_at=fixed_time,
                updated_at=fixed_time,
            )
        ])

    def _saved_state(self):
        return {
            "settlement": list(
                MonthlySettlement.objects.filter(pk=self.settlement.pk).values(*MONTHLY_FIELDS)
            ),
            "coach_rows": list(
                CoachMonthlySettlement.objects.filter(
                    monthly_settlement=self.settlement
                ).order_by("pk").values(*COACH_FIELDS)
            ),
            "payments": list(
                SettlementPayment.objects.filter(
                    monthly_settlement=self.settlement
                ).order_by("pk").values(*PAYMENT_FIELDS)
            ),
        }

    def _assert_closed_recalculation_is_read_only(self, *, force):
        before = self._saved_state()
        with (
            patch("club.settlement_service.sync_legacy_payouts_through") as sync,
            patch("club.settlement_service.load_monthly_settlement_data") as load,
            patch("club.settlement_service.persist_monthly_settlement") as persist,
        ):
            result = calculate_monthly_settlement(2026, 7, force=force)

        self.assertTrue(result["is_closed"])
        self.assertEqual(result["settlement"].calculation_snapshot, before["settlement"][0]["calculation_snapshot"])
        self.assertEqual(self._saved_state(), before)
        sync.assert_not_called()
        load.assert_not_called()
        persist.assert_not_called()

    def test_closed_month_without_force_preserves_every_saved_field(self):
        self._assert_closed_recalculation_is_read_only(force=False)

    def test_closed_month_with_force_preserves_every_saved_field(self):
        self._assert_closed_recalculation_is_read_only(force=True)

    def test_admin_view_preserves_closed_month(self):
        before = self._saved_state()
        self.client.force_login(self.admin)

        response = self.client.get(
            reverse("club:coach_admin_settlement"),
            {"year": 2026, "month": 7},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._saved_state(), before)


class OpenSettlementRecalculationTests(TestCase):
    def test_open_month_recalculates_with_and_without_force(self):
        for month, force in ((5, False), (6, True)):
            with self.subTest(force=force):
                settlement = MonthlySettlement.objects.create(
                    year=2099,
                    month=month,
                    status=MonthlySettlement.STATUS_DRAFT,
                    calculation_snapshot={"before": True},
                )

                calculate_monthly_settlement(2099, month, force=force)

                settlement.refresh_from_db()
                self.assertNotEqual(settlement.calculation_snapshot, {"before": True})
