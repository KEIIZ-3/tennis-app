from datetime import datetime
from types import SimpleNamespace

from django.test import SimpleTestCase
from django.utils import timezone

from club.settlement_balance_policy import (
    _automatic_court_cost,
    _court_transfer_allocation,
    _held_execution_reservations,
    _lighting_start_hour,
)


class SettlementWalletCourtCostTests(SimpleTestCase):
    def _reservation(self, start_at, end_at, court_count=1):
        return SimpleNamespace(
            start_at=timezone.make_aware(start_at),
            end_at=timezone.make_aware(end_at),
            court_count=court_count,
        )

    def test_lighting_start_hour_by_season(self):
        self.assertEqual(_lighting_start_hour(datetime(2026, 3, 1).date()), 18)
        self.assertEqual(_lighting_start_hour(datetime(2026, 5, 1).date()), 19)
        self.assertEqual(_lighting_start_hour(datetime(2026, 9, 30).date()), 18)
        self.assertEqual(_lighting_start_hour(datetime(2026, 10, 1).date()), 17)
        self.assertEqual(_lighting_start_hour(datetime(2026, 2, 28).date()), 17)

    def test_weekday_two_hour_court_without_lighting(self):
        reservation = self._reservation(
            datetime(2026, 7, 1, 15, 0),
            datetime(2026, 7, 1, 17, 0),
        )

        self.assertEqual(_automatic_court_cost(reservation), 1800)

    def test_weekday_two_hour_court_with_summer_lighting(self):
        reservation = self._reservation(
            datetime(2026, 7, 1, 19, 0),
            datetime(2026, 7, 1, 21, 0),
        )

        self.assertEqual(_automatic_court_cost(reservation), 2600)

    def test_weekend_two_hour_court_with_summer_lighting(self):
        reservation = self._reservation(
            datetime(2026, 7, 4, 19, 0),
            datetime(2026, 7, 4, 21, 0),
        )

        self.assertEqual(_automatic_court_cost(reservation), 3200)

    def test_multiple_courts_are_multiplied(self):
        reservation = self._reservation(
            datetime(2026, 7, 4, 19, 0),
            datetime(2026, 7, 4, 21, 0),
            court_count=2,
        )

        self.assertEqual(_automatic_court_cost(reservation), 6400)

    def test_court_transfer_is_applied_in_wallet_policy(self):
        expense = SimpleNamespace(pk=10)
        allocation = _court_transfer_allocation(
            [
                {
                    "expense": expense,
                    "amount": 1001,
                    "meta": {
                        "record_kind": "court_transfer",
                        "payer_coach_id": "3",
                        "using_coach_ids": [1, "2", 2, 999],
                    },
                }
            ],
            eligible_coach_ids=[1, 2, 3],
        )

        self.assertEqual(allocation["burden_by_coach"], {1: 501, 2: 500})
        self.assertEqual(allocation["reimbursement_by_coach"], {3: 1001})
        self.assertEqual(allocation["expense_ids"], {10})
        self.assertEqual(allocation["total"], 1001)

    def test_non_transfer_court_expense_is_not_allocated_twice(self):
        allocation = _court_transfer_allocation(
            [
                {
                    "expense": SimpleNamespace(pk=11),
                    "amount": 2400,
                    "meta": {
                        "expense_type": "common",
                        "payer_coach_id": 3,
                        "using_coach_ids": [1, 2],
                    },
                }
            ],
            eligible_coach_ids=[1, 2, 3],
        )

        self.assertEqual(allocation["burden_by_coach"], {})
        self.assertEqual(allocation["reimbursement_by_coach"], {})
        self.assertEqual(allocation["expense_ids"], set())
        self.assertEqual(allocation["total"], 0)

    def test_only_held_execution_slots_are_eligible_once(self):
        start_at = timezone.make_aware(datetime(2026, 7, 4, 19, 0))
        held_first = SimpleNamespace(
            pk=1,
            start_at=start_at,
            fixed_lesson=SimpleNamespace(pk=10),
            availability=None,
        )
        held_duplicate = SimpleNamespace(
            pk=2,
            start_at=start_at,
            fixed_lesson=SimpleNamespace(pk=10),
            availability=None,
        )
        scheduled = SimpleNamespace(
            pk=3,
            start_at=start_at,
            fixed_lesson=None,
            availability=SimpleNamespace(pk=20),
        )

        eligible = _held_execution_reservations(
            [held_first, held_duplicate, scheduled],
            {
                "fixed:10:2026-07-04": {"status": "held"},
                "availability:20": {"status": "scheduled"},
            },
        )

        self.assertEqual(eligible, [held_first])
