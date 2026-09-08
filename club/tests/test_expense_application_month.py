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

    @patch("club.views.timezone.localdate", return_value=date(2026, 9, 8))
    def test_history_groups_by_category_in_choice_order_with_subtotals(self, _localdate):
        created = [
            self.create_expense(expense_date=date(2026, 9, 10), amount=5000,
                                category=CoachExpense.CATEGORY_BALL,
                                start=date(2026, 9, 1), end=date(2026, 9, 1)),
            self.create_expense(expense_date=date(2026, 9, 12), amount=3000,
                                category=CoachExpense.CATEGORY_BALL,
                                start=date(2026, 9, 1), end=date(2026, 9, 1)),
            self.create_expense(expense_date=date(2026, 9, 15), amount=2000,
                                category=CoachExpense.CATEGORY_COURT),
            self.create_expense(expense_date=date(2026, 9, 22), amount=2500,
                                category=CoachExpense.CATEGORY_COURT),
            self.create_expense(expense_date=date(2026, 9, 18), amount=1000,
                                category=CoachExpense.CATEGORY_OTHER),
        ]
        self.client.force_login(self.admin)

        response = self.client.get(self.url)
        month_group = response.context["expense_month_groups"][1]
        category_groups = month_group["category_groups"]

        self.assertEqual(
            [group["value"] for group in category_groups],
            [CoachExpense.CATEGORY_COURT, CoachExpense.CATEGORY_BALL,
             CoachExpense.CATEGORY_OTHER],
        )
        self.assertEqual([group["subtotal"] for group in category_groups], [4500, 8000, 1000])
        self.assertEqual(month_group["total"], 13500)
        self.assertEqual(sum(group["subtotal"] for group in category_groups), month_group["total"])
        self.assertEqual(
            [row["expense"].id for row in category_groups[0]["rows"]],
            [created[3].id, created[2].id],
        )
        self.assertEqual(
            [row["expense"].category for group in category_groups for row in group["rows"]],
            [CoachExpense.CATEGORY_COURT] * 2
            + [CoachExpense.CATEGORY_BALL] * 2
            + [CoachExpense.CATEGORY_OTHER],
        )

    @patch("club.views.timezone.localdate", return_value=date(2026, 9, 8))
    def test_category_filter_keeps_three_months_and_does_not_filter_summary(self, _localdate):
        self.create_expense(expense_date=date(2026, 8, 20), amount=5000,
                            category=CoachExpense.CATEGORY_BALL,
                            start=date(2026, 8, 1), end=date(2026, 8, 1))
        self.create_expense(expense_date=date(2026, 9, 20), amount=2000,
                            category=CoachExpense.CATEGORY_COURT)
        self.create_expense(expense_date=date(2026, 10, 20), amount=3000,
                            category=CoachExpense.CATEGORY_BALL,
                            start=date(2026, 10, 1), end=date(2026, 10, 1))
        self.client.force_login(self.admin)

        response = self.client.get(self.url, {"category": CoachExpense.CATEGORY_BALL})
        groups = response.context["expense_month_groups"]

        self.assertEqual(len(groups), 3)
        self.assertEqual([group["total"] for group in groups], [5000, 0, 3000])
        self.assertEqual([len(group["category_groups"]) for group in groups], [1, 0, 1])
        self.assertEqual(response.context["selected_history_category"], CoachExpense.CATEGORY_BALL)
        self.assertEqual(groups[1]["total_label"], "ボール費用合計")
        self.assertEqual(response.context["current_month_total"], 2000)
        self.assertContains(response, '<option value="ball" selected>ボール費用</option>', html=True)
        self.assertContains(response, "この月のボール費用はありません。")

        court_response = self.client.get(self.url, {"category": CoachExpense.CATEGORY_COURT})
        self.assertEqual(
            [group["total"] for group in court_response.context["expense_month_groups"]],
            [0, 2000, 0],
        )

    @patch("club.views.timezone.localdate", return_value=date(2026, 9, 8))
    def test_all_and_invalid_category_filters_show_all_categories(self, _localdate):
        self.create_expense(expense_date=date(2026, 9, 10), amount=100,
                            category=CoachExpense.CATEGORY_COURT)
        self.create_expense(expense_date=date(2026, 9, 11), amount=200,
                            category=CoachExpense.CATEGORY_OTHER)
        self.client.force_login(self.admin)

        for category in ("all", "not-a-category"):
            with self.subTest(category=category):
                response = self.client.get(self.url, {"category": category})
                month_group = response.context["expense_month_groups"][1]
                self.assertEqual(response.context["selected_history_category"], "all")
                self.assertEqual(len(month_group["category_groups"]), 2)
                self.assertEqual(month_group["total"], 300)

    @patch("club.views.timezone.localdate", return_value=date(2026, 9, 8))
    def test_rows_with_same_expense_date_use_descending_id(self, _localdate):
        older_id = self.create_expense(
            expense_date=date(2026, 9, 10), amount=100,
            category=CoachExpense.CATEGORY_OTHER,
        )
        newer_id = self.create_expense(
            expense_date=date(2026, 9, 10), amount=200,
            category=CoachExpense.CATEGORY_OTHER,
        )
        self.client.force_login(self.admin)

        response = self.client.get(self.url)
        other_group = response.context["expense_month_groups"][1]["category_groups"][0]
        self.assertEqual(
            [row["expense"].id for row in other_group["rows"]],
            [newer_id.id, older_id.id],
        )

    @patch("club.views.timezone.localdate", return_value=date(2026, 9, 8))
    def test_moving_ball_changes_history_group_and_totals(self, _localdate):
        expense = self.create_expense(
            expense_date=date(2026, 9, 8), amount=7568,
            category=CoachExpense.CATEGORY_BALL,
            start=date(2026, 9, 1), end=date(2026, 9, 1),
        )
        self.client.force_login(self.admin)
        self.client.post(self.url, {
            "action": "update_meta", "expense_id": expense.pk,
            "ball_application_month": "2026-10",
        })

        response = self.client.get(self.url, {"category": CoachExpense.CATEGORY_BALL})
        groups = response.context["expense_month_groups"]
        self.assertEqual([group["total"] for group in groups], [0, 0, 7568])
        self.assertEqual(groups[1]["category_groups"], [])
        self.assertEqual(groups[2]["category_groups"][0]["subtotal"], 7568)

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
