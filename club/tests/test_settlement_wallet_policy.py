from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from club.settlement_balance_policy import (
    _apply_wallet_policy,
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

    def test_contractor_lesson_court_cost_is_shared_by_main_coaches_once(self):
        allocation = _court_transfer_allocation(
            [
                {
                    "expense": SimpleNamespace(pk=12),
                    "amount": 3000,
                    "meta": {
                        "record_kind": "court_transfer",
                        "payer_coach_id": 1,
                        "using_coach_ids": [4],
                    },
                }
            ],
            eligible_coach_ids=[1, 2, 3, 4],
            main_coach_ids=[1, 2, 3],
            contractor_coach_ids=[4],
        )

        self.assertEqual(allocation["burden_by_coach"], {1: 1000, 2: 1000, 3: 1000})
        self.assertEqual(allocation["reimbursement_by_coach"], {1: 3000})
        self.assertNotIn(4, allocation["burden_by_coach"])
        payer_net = allocation["reimbursement_by_coach"][1] - allocation["burden_by_coach"][1]
        self.assertEqual(payer_net, 2000)

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

    @patch("club.settlement_balance_policy._active_salary_payment_total", return_value=0)
    @patch("club.settlement_balance_policy._build_other_expense_policy")
    @patch("club.settlement_balance_policy._build_court_cost_policy")
    @patch("club.settlement_balance_policy.main_coaches")
    @patch("club.settlement_models.CoachMonthlySettlement.objects.filter")
    def test_unassigned_common_expense_is_not_added_back_to_salary(
        self,
        saved_row_filter,
        main_coaches_mock,
        court_policy_mock,
        other_expense_policy_mock,
        _salary_payment_mock,
    ):
        coach = SimpleNamespace(pk=1, role="coach")
        main_coaches_mock.return_value = [coach]
        saved_row_filter.return_value.first.return_value = None
        court_policy_mock.return_value = {
            "burden_by_coach": {},
            "reimbursement_by_coach": {},
            "finalized_court_cost_total": 0,
            "court_reimbursement_total": 0,
            "unmatched_expected_total": 0,
            "unused_registered_total": 0,
        }
        other_expense_policy_mock.return_value = {
            "burden_by_coach": {coach.pk: 7800},
            "reimbursement_by_coach": {},
            "expense_total": 7800,
        }
        settlement = SimpleNamespace(
            is_closed=False,
            closing_balance=0,
            calculation_snapshot={},
            save=MagicMock(),
        )
        result = {
            "settlement": settlement,
            "coach_rows": [
                {
                    "coach": coach,
                    "coach_name": "井上春佳",
                    "is_contractor_coach": False,
                    "ticket_amount": 0,
                    "preopen_paid_amount": 26000,
                    "stringing_amount": 0,
                    "contractor_hourly_pay_amount": 0,
                }
            ],
            "ticket_amount_total": 0,
            "preopen_paid_total": 26000,
            "stringing_total": 0,
        }

        updated = _apply_wallet_policy(result, 2026, 7)
        row = updated["coach_rows"][0]

        self.assertEqual(row["wallet_earned_amount"], 26000)
        self.assertEqual(row["common_expense_share"], 7800)
        self.assertEqual(row["wallet_balance_adjustment"], 0)
        self.assertEqual(row["salary_due"], 18200)
