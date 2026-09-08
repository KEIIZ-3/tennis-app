from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from club.models import CoachExpense
from club.settlement_balance_policy import _approved_monthly_expenses
from club.settlement_models import MonthlySettlement
from club.views import _expense_meta_row


class ExpenseApplicationMonthTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.admin = user_model.objects.create_user(
            username="expense-admin", password="password", is_staff=True
        )
        self.coach = user_model.objects.create_user(
            username="expense-coach", password="password", role="coach"
        )
        self.url = reverse("club:coach_expense_manage")

    def create_expense(self, *, expense_date, amount, category=CoachExpense.CATEGORY_OTHER,
                       start=None, end=None, created_by=None):
        return CoachExpense.objects.create(
            expense_date=expense_date,
            category=category,
            amount=amount,
            settlement_period_start=start,
            settlement_period_end=end,
            created_by=created_by or self.admin,
        )

    def test_canonical_application_month_uses_date_for_normal_and_start_for_ball(self):
        normal = self.create_expense(expense_date=date(2026, 9, 15), amount=100)
        ball = self.create_expense(
            expense_date=date(2026, 9, 15), amount=200,
            category=CoachExpense.CATEGORY_BALL,
            start=date(2026, 8, 1), end=date(2026, 10, 1),
        )

        self.assertEqual(_expense_meta_row(normal)["application_month"], date(2026, 9, 1))
        ball_row = _expense_meta_row(ball)
        self.assertEqual(ball_row["application_month"], date(2026, 8, 1))
        self.assertEqual(ball_row["ball_application_month_label"], "2026年8月")

    @patch("club.views.timezone.localdate", return_value=date(2026, 9, 8))
    def test_history_has_three_application_month_groups_and_totals(self, _localdate):
        for expense_date, amount in (
            (date(2026, 7, 15), 1), (date(2026, 8, 15), 10),
            (date(2026, 9, 15), 20), (date(2026, 10, 15), 30),
            (date(2026, 11, 15), 2),
        ):
            self.create_expense(expense_date=expense_date, amount=amount)
        self.create_expense(
            expense_date=date(2026, 9, 20), amount=40,
            category=CoachExpense.CATEGORY_BALL,
            start=date(2026, 8, 1), end=date(2026, 10, 1),
        )
        self.client.force_login(self.admin)

        response = self.client.get(self.url)
        groups = response.context["expense_month_groups"]

        self.assertEqual([group["month"] for group in groups], [
            date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1)
        ])
        self.assertEqual([group["total"] for group in groups], [50, 20, 30])
        self.assertEqual(response.context["current_month_total"], 20)

    def test_create_ball_uses_one_application_month_for_both_internal_fields(self):
        self.client.force_login(self.admin)
        response = self.client.post(self.url, data={
            "action": "create", "expense_date": "2026-09-08", "expense_type": "common",
            "category": "ball", "amount": "7568", "receipt_status": "none",
            "receipt_check_status": "unchecked", "approval_status": "approved",
            "ball_application_month": "2026-10",
        })
        self.assertEqual(response.status_code, 302)
        expense = CoachExpense.objects.get()
        self.assertEqual(expense.settlement_period_start, date(2026, 10, 1))
        self.assertEqual(expense.settlement_period_end, date(2026, 10, 1))

    def test_admin_can_move_ball_and_non_admin_post_cannot(self):
        expense = self.create_expense(
            expense_date=date(2026, 9, 8), amount=7568,
            category=CoachExpense.CATEGORY_BALL,
            start=date(2026, 9, 1), end=date(2026, 9, 1),
        )
        payload = {
            "action": "update_meta", "expense_id": expense.pk,
            "ball_application_month": "2026-10",
        }
        self.client.force_login(self.coach)
        self.client.post(self.url, payload)
        expense.refresh_from_db()
        self.assertEqual(expense.settlement_period_start, date(2026, 9, 1))

        self.client.force_login(self.admin)
        self.client.post(self.url, payload)
        expense.refresh_from_db()
        self.assertEqual(expense.settlement_period_start, date(2026, 10, 1))
        self.assertEqual(expense.settlement_period_end, date(2026, 10, 1))

    def test_move_rejects_closed_old_or_new_month_without_database_change(self):
        for closed_month in (9, 10):
            with self.subTest(closed_month=closed_month):
                expense = self.create_expense(
                    expense_date=date(2026, 11, 8), amount=100,
                    category=CoachExpense.CATEGORY_BALL,
                    start=date(2026, 9, 1), end=date(2026, 9, 1),
                )
                MonthlySettlement.objects.create(
                    year=2026, month=closed_month, status=MonthlySettlement.STATUS_CLOSED
                )
                self.client.force_login(self.admin)
                self.client.post(self.url, {
                    "action": "update_meta", "expense_id": expense.pk,
                    "ball_application_month": "2026-10",
                })
                expense.refresh_from_db()
                self.assertEqual(expense.settlement_period_start, date(2026, 9, 1))
                MonthlySettlement.objects.all().delete()
                expense.delete()

    def test_legacy_ball_is_counted_once_in_start_month(self):
        self.create_expense(
            expense_date=date(2026, 9, 8), amount=7568,
            category=CoachExpense.CATEGORY_BALL,
            start=date(2026, 7, 1), end=date(2026, 9, 1),
        )
        july = _approved_monthly_expenses(date(2026, 7, 1), date(2026, 8, 1))
        august = _approved_monthly_expenses(date(2026, 8, 1), date(2026, 9, 1))
        self.assertEqual([row["amount"] for row in july], [7568])
        self.assertEqual(august, [])
