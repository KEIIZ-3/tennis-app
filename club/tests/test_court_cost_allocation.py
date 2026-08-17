from django.test import SimpleTestCase

from club.court_cost_allocation import allocate_court_cost


class CourtCostAllocationTests(SimpleTestCase):
    def allocate(self, amount, using_ids, contractor_ids=()):
        return allocate_court_cost(
            amount,
            using_ids,
            main_coach_ids=[1, 2, 3],
            contractor_coach_ids=contractor_ids,
        )["burden_by_coach"]

    def test_one_main_coach_bears_full_cost(self):
        self.assertEqual(self.allocate(2600, [1]), {1: 2600})

    def test_two_main_coaches_split_cost(self):
        self.assertEqual(self.allocate(2600, [1, 2]), {1: 1300, 2: 1300})

    def test_three_main_coaches_split_cost(self):
        self.assertEqual(self.allocate(3000, [1, 2, 3]), {1: 1000, 2: 1000, 3: 1000})

    def test_remainder_is_deterministic_and_total_is_preserved(self):
        allocation = self.allocate(2600, [1, 2, 3])
        self.assertEqual(allocation, {1: 867, 2: 867, 3: 866})
        self.assertEqual(sum(allocation.values()), 2600)

    def test_contractor_only_is_shared_by_all_main_coaches(self):
        allocation = self.allocate(3000, [4], contractor_ids=[4])
        self.assertEqual(allocation, {1: 1000, 2: 1000, 3: 1000})
        self.assertNotIn(4, allocation)

    def test_mixed_lesson_excludes_contractor(self):
        self.assertEqual(
            self.allocate(2600, [1, 4], contractor_ids=[4]),
            {1: 2600},
        )
