from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from club.templatetags.settlement_breakdown import (
    _common_expense_rows,
    common_expense_breakdown,
)


class CommonExpenseBreakdownTests(SimpleTestCase):
    @patch("club.templatetags.settlement_breakdown.CoachExpense.objects.filter")
    def test_detail_uses_saved_common_expense_allocations(
        self,
        expense_filter,
    ):
        expense_filter.return_value.select_related.return_value = []
        snapshot = {
            "other_expense_policy": {
                "detail_rows": [
                    {
                        "expense_id": 1,
                        "amount": 7568,
                        "burden_target_ids": [1, 2, 3],
                        "burden_by_coach": {1: 2366, 2: 2112, 3: 3090},
                    },
                    {
                        "expense_id": 2,
                        "amount": 7801,
                        "burden_target_ids": [1, 2, 3],
                    },
                ]
            }
        }

        coach_two_rows, _policy = _common_expense_rows(snapshot, coach_id=2)

        self.assertEqual(
            [row["own_amount"] for row in coach_two_rows],
            [2112, 2600],
        )

    @patch("club.templatetags.settlement_breakdown.CoachExpense.objects.filter")
    def test_breakdown_uses_saved_profit_base_for_display(self, expense_filter):
        expense_filter.return_value.select_related.return_value = []
        settlement = SimpleNamespace(
            calculation_snapshot={
                "main_coach_ids": [1, 2, 3],
                "main_coach_names": ["A", "B", "C"],
                "common_expense_profit_base_by_coach": {
                    "1": 100000,
                    "2": 60000,
                    "3": 40000,
                },
                "other_expense_policy": {"detail_rows": []},
            }
        )

        context = common_expense_breakdown(settlement)

        self.assertEqual(
            [item["profit_base"] for item in context["allocation_bases"]],
            [100000, 60000, 40000],
        )
        self.assertEqual(
            [item["rate"] for item in context["allocation_bases"]],
            [50.0, 30.0, 20.0],
        )
