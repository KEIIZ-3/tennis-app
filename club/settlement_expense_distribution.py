def build_expense_distribution_policies(
    *,
    year,
    month,
    main_coach_ids,
    eligible_coach_ids,
    contractor_coach_ids,
    build_court_cost_policy,
    build_other_expense_policy,
    held_participant_count_by_coach,
    build_rain_refund_policy,
):
    court_policy = build_court_cost_policy(
        year,
        month,
        main_coach_ids,
        eligible_coach_ids,
        contractor_coach_ids,
    )
    other_expense_policy = build_other_expense_policy(
        year,
        month,
        main_coach_ids,
        held_participant_count_by_coach(year, month, main_coach_ids),
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
