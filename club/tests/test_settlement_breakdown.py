from unittest.mock import patch

from django.test import SimpleTestCase

from club.templatetags.settlement_breakdown import _common_expense_rows


class CommonExpenseBreakdownTests(SimpleTestCase):
    @patch("club.templatetags.settlement_breakdown.CoachExpense.objects.filter")
    def test_detail_uses_saved_ball_allocation_and_keeps_other_expense_even(
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
