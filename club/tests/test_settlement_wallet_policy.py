from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from club.settlement_balance_policy import (
    _apply_wallet_policy,
    _automatic_court_cost,
    _ball_expense_amount_for_month,
    _build_other_expense_policy,
    _court_transfer_allocation,
    _held_execution_reservations,
    _held_participant_count_by_coach,
    _lighting_start_hour,
    _negative_carry_in_by_coach,
    _rain_refund_policy,
    _unpaid_salary_carry_in_by_coach,
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

    def test_ball_expense_uses_only_selected_settlement_month(self):
        expense = SimpleNamespace(
            amount=37840,
            expense_date=datetime(2026, 5, 10).date(),
            settlement_period_start=date(2026, 7, 1),
            settlement_period_end=date(2026, 9, 1),
        )
        meta = {}

        july = _ball_expense_amount_for_month(
            expense,
            meta,
            datetime(2026, 7, 1).date(),
            datetime(2026, 8, 1).date(),
        )
        august = _ball_expense_amount_for_month(
            expense,
            meta,
            datetime(2026, 8, 1).date(),
            datetime(2026, 9, 1).date(),
        )
        september = _ball_expense_amount_for_month(
            expense,
            meta,
            datetime(2026, 9, 1).date(),
            datetime(2026, 10, 1).date(),
        )
        october = _ball_expense_amount_for_month(
            expense,
            meta,
            datetime(2026, 10, 1).date(),
            datetime(2026, 11, 1).date(),
        )

        self.assertEqual((july, august, september), (12614, 12613, 12613))
        self.assertEqual(july + august + september, 37840)
        self.assertIsNone(october)

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

    @patch("club.settlement_models.CoachMonthlySettlement.objects.filter")
    def test_negative_carry_uses_only_previous_closed_month(self, filter_mock):
        filter_mock.return_value.values.return_value = [
            {
                "coach_id": 2,
                "calculation_snapshot": {"negative_carry": 2080},
            }
        ]

        carry = _negative_carry_in_by_coach(2026, 8, [1, 2, 3])

        self.assertEqual(carry, {2: 2080})
        filter_mock.assert_called_once_with(
            monthly_settlement__year=2026,
            monthly_settlement__month=7,
            monthly_settlement__status="closed",
            coach_id__in=[1, 2, 3],
        )

    @patch("club.settlement_models.CoachMonthlySettlement.objects.filter")
    def test_negative_carry_crosses_year_boundary(self, filter_mock):
        filter_mock.return_value.values.return_value = []

        carry = _negative_carry_in_by_coach(2027, 1, [2])

        self.assertEqual(carry, {})
        filter_mock.assert_called_once_with(
            monthly_settlement__year=2026,
            monthly_settlement__month=12,
            monthly_settlement__status="closed",
            coach_id__in=[2],
        )

    @patch("club.settlement_models.CoachMonthlySettlement.objects.filter")
    def test_unpaid_salary_carry_uses_previous_closed_month(self, filter_mock):
        filter_mock.return_value.values.return_value = [
            {"coach_id": 1, "salary_unpaid": 221},
            {"coach_id": 2, "salary_unpaid": 0},
        ]

        carry = _unpaid_salary_carry_in_by_coach(2026, 8, [1, 2, 3])

        self.assertEqual(carry, {1: 221})
        filter_mock.assert_called_once_with(
            monthly_settlement__year=2026,
            monthly_settlement__month=7,
            monthly_settlement__status="closed",
            coach_id__in=[1, 2, 3],
        )

    def test_ball_expense_without_target_month_is_not_counted(self):
        expense = SimpleNamespace(
            amount=7568,
            expense_date=datetime(2026, 7, 25).date(),
            settlement_period_start=None,
            settlement_period_end=None,
        )

        self.assertIsNone(
            _ball_expense_amount_for_month(
                expense,
                {},
                datetime(2026, 7, 1).date(),
                datetime(2026, 8, 1).date(),
            )
        )

    def test_future_ball_target_month_is_not_counted_in_past(self):
        expense = SimpleNamespace(
            amount=7568,
            expense_date=datetime(2026, 7, 25).date(),
            settlement_period_start=date(2026, 8, 1),
            settlement_period_end=date(2026, 8, 1),
        )
        meta = {}

        self.assertIsNone(
            _ball_expense_amount_for_month(
                expense,
                meta,
                datetime(2026, 7, 1).date(),
                datetime(2026, 8, 1).date(),
            )
        )
        self.assertEqual(
            _ball_expense_amount_for_month(
                expense,
                meta,
                datetime(2026, 8, 1).date(),
                datetime(2026, 9, 1).date(),
            ),
            7568,
        )

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

    @patch("club.settlement_balance_policy._approved_monthly_expenses")
    def test_personal_business_expense_is_excluded_from_payroll(self, expenses_mock):
        expenses_mock.return_value = [
            {
                "expense": SimpleNamespace(pk=20),
                "amount": 14740,
                "payer_id": 1,
                "expense_type": "personal",
                "is_court": False,
            },
            {
                "expense": SimpleNamespace(pk=21),
                "amount": 7800,
                "payer_id": None,
                "expense_type": "common",
                "is_court": False,
            },
        ]

        policy = _build_other_expense_policy(2026, 7, [1, 2, 3])

        self.assertEqual(policy["expense_total"], 7800)
        self.assertEqual(policy["burden_by_coach"], {1: 2600, 2: 2600, 3: 2600})
        self.assertEqual(policy["ball_burden_by_coach"], {})
        self.assertEqual(
            policy["other_burden_by_coach"],
            {1: 2600, 2: 2600, 3: 2600},
        )
        self.assertEqual(policy["reimbursement_by_coach"], {})
        self.assertEqual([row["expense_id"] for row in policy["detail_rows"]], [21])

    @patch("club.settlement_balance_policy._approved_monthly_expenses")
    def test_ball_expense_is_split_by_held_participant_count(self, expenses_mock):
        expenses_mock.return_value = [
            {
                "expense": SimpleNamespace(pk=22, category="ball"),
                "amount": 7801,
                "payer_id": 1,
                "expense_type": "common",
                "is_court": False,
            },
        ]

        policy = _build_other_expense_policy(
            2026,
            7,
            [1, 2, 3],
            {1: 3, 2: 2, 3: 1},
        )

        self.assertEqual(policy["burden_by_coach"], {1: 3901, 2: 2600, 3: 1300})
        self.assertEqual(
            policy["ball_burden_by_coach"],
            {1: 3901, 2: 2600, 3: 1300},
        )
        self.assertEqual(policy["other_burden_by_coach"], {})
        self.assertEqual(policy["ball_reimbursement_by_coach"], {1: 3900})
        self.assertEqual(policy["other_reimbursement_by_coach"], {})
        self.assertEqual(policy["reimbursement_by_coach"], {1: 3900})
        self.assertEqual(policy["reimbursement_total"], 3900)
        self.assertEqual(sum(policy["burden_by_coach"].values()), 7801)
        self.assertEqual(
            policy["detail_rows"][0]["burden_rule"],
            "完了済みレッスンの担当参加人数に比例",
        )

    @patch("club.settlement_balance_policy._approved_monthly_expenses")
    def test_july_ball_expense_7568_uses_participant_ratio(self, expenses_mock):
        expenses_mock.return_value = [
            {
                "expense": SimpleNamespace(pk=23, category="ball"),
                "amount": 7568,
                "payer_id": 1,
                "expense_type": "common",
                "is_court": False,
            },
        ]

        policy = _build_other_expense_policy(
            2026,
            7,
            [1, 2, 3],
            {1: 14, 2: 4, 3: 2},
        )

        self.assertEqual(
            policy["ball_burden_by_coach"],
            {1: 5298, 2: 1514, 3: 756},
        )
        self.assertEqual(policy["ball_reimbursement_by_coach"], {1: 2270})
        self.assertEqual(policy["reimbursement_by_coach"], {1: 2270})
        self.assertEqual(sum(policy["ball_burden_by_coach"].values()), 7568)

    @patch("club.models.RainRefund.objects.filter")
    def test_rain_refund_waits_then_transfers_after_confirmation(
        self,
        filter_mock,
    ):
        payer = SimpleNamespace(display_name=lambda: "飯塚研太朗")
        collection = SimpleNamespace(display_name=lambda: "清水峻平")
        pending_refund = SimpleNamespace(
            expense_id=30,
            lesson_date=date(2026, 7, 12),
            amount=2600,
            lesson_label="7月12日レッスン",
            account_name="外部予約サイト",
            collection_coach_id=2,
            collection_coach=collection,
            payer_coach=payer,
            payer_coach_id=1,
            debit_coach_id=2,
            status="pending",
        )
        refunded_refund = SimpleNamespace(
            expense_id=31,
            lesson_date=date(2026, 7, 19),
            amount=3200,
            lesson_label="7月19日レッスン",
            account_name="外部予約サイト",
            collection_coach_id=2,
            collection_coach=collection,
            payer_coach=payer,
            payer_coach_id=1,
            debit_coach_id=2,
            status="refunded",
        )
        filter_mock.return_value.select_related.return_value.order_by.return_value = [
            pending_refund,
            refunded_refund,
        ]

        policy = _rain_refund_policy(2026, 7, [1, 2, 3])

        self.assertEqual(policy["pending_total"], 2600)
        self.assertEqual(policy["burden_by_coach"], {2: 3200})
        self.assertEqual(policy["reimbursement_by_coach"], {1: 3200})
        self.assertEqual(policy["refunded_total"], 3200)
        self.assertEqual(
            sum(policy["reimbursement_by_coach"].values())
            - sum(policy["burden_by_coach"].values()),
            0,
        )

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

    @patch(
        "club.settlement_balance_policy."
        "_monthly_execution_reservations_and_status"
    )
    def test_ball_participant_count_uses_finished_non_rain_canceled_lessons(
        self,
        execution_rows_mock,
    ):
        coach_1 = SimpleNamespace(pk=1, role="coach")
        coach_2 = SimpleNamespace(pk=2, role="coach")
        held_member_1 = SimpleNamespace(
            pk=1,
            user_id=101,
            start_at=timezone.make_aware(datetime(2026, 7, 4, 19, 0)),
            fixed_lesson=SimpleNamespace(
                pk=10,
                all_coaches=lambda: [coach_1, coach_2],
            ),
            availability=None,
            substitute_coach=None,
        )
        held_member_2 = SimpleNamespace(
            pk=2,
            user_id=102,
            start_at=timezone.make_aware(datetime(2026, 7, 4, 19, 0)),
            fixed_lesson=SimpleNamespace(
                pk=10,
                all_coaches=lambda: [coach_1, coach_2],
            ),
            availability=None,
            substitute_coach=None,
        )
        scheduled_member = SimpleNamespace(
            pk=3,
            user_id=103,
            start_at=timezone.make_aware(datetime(2026, 7, 5, 19, 0)),
            fixed_lesson=SimpleNamespace(
                pk=11,
                all_coaches=lambda: [coach_1],
            ),
            availability=None,
            substitute_coach=None,
        )
        rain_canceled_member = SimpleNamespace(
            pk=4,
            user_id=104,
            start_at=timezone.make_aware(datetime(2026, 7, 6, 19, 0)),
            fixed_lesson=SimpleNamespace(
                pk=12,
                all_coaches=lambda: [coach_2],
            ),
            availability=None,
            substitute_coach=None,
        )
        execution_rows_mock.return_value = (
            [
                held_member_1,
                held_member_2,
                scheduled_member,
                rain_canceled_member,
            ],
            {
                "fixed:10:2026-07-04": {"status": "held"},
                "fixed:11:2026-07-05": {"status": "scheduled"},
                "fixed:12:2026-07-06": {"status": "rain_canceled"},
            },
        )

        counts = _held_participant_count_by_coach(2026, 7, [1, 2, 3])

        self.assertEqual(counts, {1: 3, 2: 2})
        execution_rows_mock.assert_called_once_with(2026, 7)

    @patch("club.settlement_balance_policy._active_salary_payment_total", return_value=0)
    @patch("club.settlement_balance_policy._active_reimbursement_payment_total", return_value=2000)
    @patch("club.settlement_balance_policy._unpaid_salary_carry_in_by_coach", return_value={1: 221})
    @patch("club.settlement_balance_policy._negative_carry_in_by_coach", return_value={1: 2080})
    @patch(
        "club.settlement_balance_policy._rain_refund_policy",
        return_value={
            "burden_by_coach": {},
            "reimbursement_by_coach": {},
            "pending_rows": [{"amount": 2600}],
            "pending_total": 2600,
            "refunded_rows": [],
            "refunded_total": 0,
        },
    )
    @patch("club.settlement_balance_policy._held_participant_count_by_coach", return_value={1: 1})
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
        _held_lesson_count_mock,
        _rain_refund_mock,
        _negative_carry_mock,
        _unpaid_salary_carry_mock,
        _reimbursement_payment_mock,
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
            "ball_burden_by_coach": {coach.pk: 3000},
            "other_burden_by_coach": {coach.pk: 4800},
            "ball_reimbursement_by_coach": {coach.pk: 3000},
            "other_reimbursement_by_coach": {},
            "reimbursement_by_coach": {coach.pk: 3000},
            "expense_total": 7800,
        }
        settlement = SimpleNamespace(
            is_closed=False,
            opening_balance=6000,
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
        self.assertEqual(row["ball_expense_burden"], 3000)
        self.assertEqual(row["ball_expense_reimbursement"], 3000)
        self.assertEqual(row["other_expense_burden"], 4800)
        self.assertEqual(row["common_expense_share"], 7800)
        self.assertEqual(row["wallet_balance_adjustment"], 0)
        self.assertEqual(row["negative_carry_in"], 2080)
        self.assertEqual(row["unpaid_salary_carry_in"], 221)
        self.assertEqual(row["salary_due"], 19341)
        self.assertEqual(row["reimbursement_paid"], 2000)
        self.assertEqual(row["unpaid_salary"], 17341)
        self.assertEqual(updated["cash_out_total"], 2000)
        self.assertEqual(updated["opening_balance"], 6000)
        self.assertEqual(updated["company_balance"], 30000)
        self.assertEqual(
            settlement.calculation_snapshot["company_internal_reserve"],
            6000,
        )
        self.assertEqual(updated["rain_refund_pending_rows"], [{"amount": 2600}])
        self.assertEqual(updated["rain_refund_pending_total"], 2600)
        self.assertEqual(updated["rain_refunded_rows"], [])
        self.assertEqual(updated["rain_refunded_total"], 0)
