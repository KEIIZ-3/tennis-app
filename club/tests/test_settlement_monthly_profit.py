from copy import deepcopy

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from club.settlement_monthly_profit import build_monthly_profit_rows


class MonthlyProfitCalculationTests(SimpleTestCase):
    def row(self, **overrides):
        row = {
            "coach_name": "テストコーチ",
            "is_main_coach": True,
            "ticket_amount": 10000,
            "preopen_paid_amount": 0,
            "stringing_amount": 0,
            "court_cost_burden": 0,
            "ball_expense_burden": 0,
            "other_expense_burden": 0,
            "rain_refund_burden": 0,
            "contractor_cost_burden": 0,
        }
        row.update(overrides)
        return row

    def profit(self, **overrides):
        return build_monthly_profit_rows([self.row(**overrides)])[0]

    def test_revenue_only_equals_profit(self):
        self.assertEqual(self.profit()["monthly_profit"], 10000)

    def test_each_official_revenue_and_burden_is_used_once(self):
        result = self.profit(
            preopen_paid_amount=2000,
            stringing_amount=3000,
            court_cost_burden=1000,
            ball_expense_burden=400,
            other_expense_burden=300,
            rain_refund_burden=200,
            contractor_cost_burden=500,
        )
        self.assertEqual(result["revenue_total"], 15000)
        self.assertEqual(result["common_expense_burden"], 900)
        self.assertEqual(result["expense_total"], 2400)
        self.assertEqual(result["monthly_profit"], 12600)

    def test_shop_allocation_is_added_once(self):
        row = self.row()
        row["coach"] = type("Coach", (), {"pk": 7})()
        result = build_monthly_profit_rows([row], {7: 36100})[0]
        self.assertEqual(result["shop_revenue"], 36100)
        self.assertEqual(result["revenue_total"], 46100)
        self.assertEqual(result["monthly_profit"], 46100)

    def test_settlement_cash_flow_fields_do_not_change_profit(self):
        base = self.row()
        expected = build_monthly_profit_rows([base])
        excluded_fields = {
            "wallet_reimbursement": 9000,
            "reimbursement_current_month": 8000,
            "reimbursement_due": 7000,
            "reimbursement_paid": 6000,
            "reimbursement_unpaid": 5000,
            "unpaid_salary_carry_in": 4000,
            "negative_carry_in": 3000,
            "salary_due": 2000,
            "salary_paid": 1000,
            "unpaid_salary": 900,
            "opening_balance": 800,
            "closing_balance": 700,
        }
        changed = deepcopy(base)
        changed.update(excluded_fields)
        self.assertEqual(build_monthly_profit_rows([changed]), expected)

    def test_contractor_rows_are_not_displayed(self):
        self.assertEqual(
            build_monthly_profit_rows(
                [self.row(is_main_coach=False, is_contractor_coach=True)]
            ),
            [],
        )


class MonthlyProfitTemplateTests(SimpleTestCase):
    def test_mobile_compatible_template_renders_profit_section(self):
        html = render_to_string(
            "coach/admin_settlement.html",
            {
                "monthly_profit_rows": [
                    {
                        "coach_name": "テストコーチ",
                        "ticket_revenue": 1000,
                        "cash_revenue": 200,
                        "stringing_revenue": 300,
                        "shop_revenue": 0,
                        "revenue_total": 1500,
                        "court_cost_burden": 400,
                        "common_expense_burden": 100,
                        "contractor_burden": 0,
                        "expense_total": 500,
                        "monthly_profit": 1000,
                    }
                ],
                "coach_rows": [],
                "wallet_revenue_total": 0,
                "cash_in_total": 0,
            },
        )
        self.assertIn("コーチ別 月間利益", html)
        self.assertIn("内訳を表示", html)
        self.assertIn("@media(max-width:768px)", html)
        self.assertIn("月間利益 1000円", html)
