from django.template.loader import render_to_string
from django.test import SimpleTestCase

from club.templatetags.settlement_breakdown import reimbursement_breakdown


class ReimbursementBreakdownTests(SimpleTestCase):
    def render(self, **row):
        return render_to_string(
            "coach/_reimbursement_breakdown.html",
            reimbursement_breakdown(row),
        )

    def test_iizuka_breakdown_matches_canonical_total_without_plus_signs(self):
        html = self.render(
            wallet_reimbursement=72799,
            court_reimbursement=17200,
            ball_expense_reimbursement=1907,
            other_expense_reimbursement=0,
            rain_refund_reimbursement=2400,
            shop_procurement_reimbursement=51292,
        )

        for label, amount in (
            ("コート代立替返還", 17200),
            ("ボール代立替返還", 1907),
            ("雨天中止コート代返金", 2400),
            ("Shop仕入立替返還", 51292),
        ):
            self.assertIn(label, html)
            self.assertIn(f">{amount}円</strong>", html)
            self.assertNotIn(f">+{amount}円</strong>", html)
        self.assertNotIn("その他経費立替返還", html)
        self.assertNotIn("立替内訳と合計が一致していません", html)

    def test_shimizu_and_inoue_breakdowns_match_their_totals(self):
        for row, expected_amounts in (
            (
                {"wallet_reimbursement": 2600, "court_reimbursement": 2600},
                (2600,),
            ),
            (
                {
                    "wallet_reimbursement": 10400,
                    "court_reimbursement": 7800,
                    "ball_expense_reimbursement": 2600,
                },
                (7800, 2600),
            ),
        ):
            context = reimbursement_breakdown(row)
            self.assertEqual(context["breakdown_total"], row["wallet_reimbursement"])
            self.assertFalse(context["has_mismatch"])
            self.assertEqual(
                tuple(item["amount"] for item in context["items"]),
                expected_amounts,
            )

    def test_zero_items_are_hidden_and_mismatch_is_diagnostic_only(self):
        html = self.render(
            wallet_reimbursement=100,
            court_reimbursement=0,
            ball_expense_reimbursement=0,
            other_expense_reimbursement=0,
            rain_refund_reimbursement=0,
            shop_procurement_reimbursement=0,
        )

        self.assertNotIn("立替返還", html)
        self.assertNotIn("+0円", html)
        self.assertNotIn("-0円", html)
        self.assertIn("立替内訳と合計が一致していません", html)

    def test_admin_template_keeps_canonical_values_and_groups_breakdown(self):
        source = render_to_string(
            "coach/admin_settlement.html",
            {
                "coach_rows": [
                    {
                        "coach_name": "飯塚 研太朗",
                        "wallet_reimbursement": 72799,
                        "court_reimbursement": 17200,
                        "ball_expense_reimbursement": 1907,
                        "rain_refund_reimbursement": 2400,
                        "shop_procurement_reimbursement": 51292,
                        "wallet_final_entitlement": 99999,
                        "salary_due": 99999,
                        "salary_paid": 12345,
                        "salary_carry_in": 2345,
                        "ball_expense_burden": 3000,
                        "other_expense_burden": 2661,
                    }
                ],
                "monthly_profit_rows": [],
                "cash_in_total": 0,
                "wallet_revenue_total": 0,
            },
        )

        self.assertIn("経費立替付与合計", source)
        self.assertIn("+72799円", source)
        self.assertEqual(source.count("+72799円"), 1)
        self.assertIn(">17200円</strong>", source)
        self.assertNotIn(">+17200円</strong>", source)
        self.assertIn("最終受取額</span><strong>99999円", source)
        self.assertIn("給与支払済み</span><strong>12345円", source)
        self.assertIn("前月精算繰越（＋未払い／－翌月調整）</span><strong>+2345円", source)
        self.assertIn("共通経費負担</span><strong>-5661円", source)
        self.assertIn("Shop利益分配</span><strong>+0円", source)
        self.assertIn("@media(max-width:768px)", source)
