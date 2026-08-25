from django.test import TestCase

from club.ball_expense_allocation import split_amount_by_profit
from club.settlement_expense_distribution import build_expense_distribution_policies


def split_evenly(amount, coach_ids):
    base, remainder = divmod(int(amount), len(coach_ids))
    return {
        coach_id: base + (1 if index < remainder else 0)
        for index, coach_id in enumerate(coach_ids)
    }


class BallExpenseProfitAllocationTests(TestCase):
    def allocate(self, profits, amount=10000):
        return split_amount_by_profit(
            amount,
            [1, 2, 3],
            dict(enumerate(profits, start=1)),
            money=int,
            split_evenly=split_evenly,
        )

    def test_profit_ratios(self):
        self.assertEqual(self.allocate([100000, 60000, 40000]), {1: 5000, 2: 3000, 3: 2000})
        self.assertEqual(self.allocate([50000, 30000, -10000]), {1: 6250, 2: 3750, 3: 0})
        self.assertEqual(self.allocate([50000, 0, 0]), {1: 10000, 2: 0, 3: 0})

    def test_non_positive_profits_split_evenly_and_preserve_remainder(self):
        allocation = self.allocate([0, -1, -100], amount=10000)
        self.assertEqual(allocation, {1: 3334, 2: 3333, 3: 3333})
        self.assertEqual(sum(allocation.values()), 10000)

    def test_zero_profit_coach_never_receives_rounding_adjustment(self):
        allocation = self.allocate([1, 1, 0], amount=7801)
        self.assertEqual(allocation[3], 0)
        self.assertEqual(sum(allocation.values()), 7801)

    def test_production_august_profit_ratio(self):
        allocation = self.allocate([15850, 14150, 20700], amount=7568)
        self.assertEqual(allocation, {1: 2366, 2: 2112, 3: 3090})

    def test_rounding_always_preserves_total(self):
        for amount in range(101):
            self.assertEqual(sum(self.allocate([7, 3, 1], amount).values()), amount)

    def test_distribution_subtracts_official_court_burden_from_revenue(self):
        captured = {}

        def other_policy(_year, _month, _coach_ids, profit_by_coach):
            captured.update(profit_by_coach)
            return {}

        result = build_expense_distribution_policies(
            year=2026,
            month=7,
            main_coach_ids=[1, 2, 3],
            eligible_coach_ids=[1, 2, 3, 4],
            contractor_coach_ids=[4],
            lesson_revenue_by_coach={1: 100000, 2: 60000, 3: 40000},
            build_court_cost_policy=lambda *args: {
                "burden_by_coach": {1: 50000, 2: 30000, 3: 50000},
            },
            build_other_expense_policy=other_policy,
            build_rain_refund_policy=lambda *args: {},
        )

        self.assertEqual(captured, {1: 50000, 2: 30000, 3: 0})
        self.assertIn("court_policy", result)
