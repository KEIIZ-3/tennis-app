from .common_expense_history import build_common_expense_policy
from .court_policy_reconciliation import reconcile_court_policy


def build_expense_distribution_policies(
    *,
    year,
    month,
    main_coach_ids,
    eligible_coach_ids,
    contractor_coach_ids,
    build_court_cost_policy,
    build_other_expense_policy,
    lesson_revenue_by_coach,
    build_rain_refund_policy,
):
    court_policy = build_court_cost_policy(
        year,
        month,
        main_coach_ids,
        eligible_coach_ids,
        contractor_coach_ids,
    )
    court_policy = reconcile_court_policy(
        court_policy,
        main_coach_ids=main_coach_ids,
        eligible_coach_ids=eligible_coach_ids,
        contractor_coach_ids=contractor_coach_ids,
    )
    profit_by_coach = {
        coach_id: max(
            int((lesson_revenue_by_coach or {}).get(coach_id, 0))
            - int(court_policy.get("burden_by_coach", {}).get(coach_id, 0)),
            0,
        )
        for coach_id in main_coach_ids
    }
    other_expense_policy = build_common_expense_policy(
        year=year,
        month=month,
        main_coach_ids=main_coach_ids,
        participant_count_by_coach=profit_by_coach,
        build_month_policy=build_other_expense_policy,
    )
    rain_refund_policy = build_rain_refund_policy(
        year,
        month,
        main_coach_ids,
    )

    return {
        "court_policy": court_policy,
        "other_expense_policy": other_expense_policy,
        "rain_refund_policy": rain_refund_policy,
    }
