def _money(value):
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def allocate_court_cost(
    total_amount,
    using_coach_ids,
    *,
    main_coach_ids,
    contractor_coach_ids,
):
    """Allocate one occurrence's court cost without charging contractors.

    A mixed main/contractor lesson follows the legacy settlement rule: only the
    assigned main coaches bear the cost.  A contractor-only lesson is shared by
    the configured main coaches.  Input order is retained so remainder yen are
    assigned deterministically.
    """
    amount = _money(total_amount)
    using_ids = list(dict.fromkeys(value for value in using_coach_ids if value))
    main_ids = list(dict.fromkeys(value for value in main_coach_ids if value))
    contractor_ids = set(contractor_coach_ids or [])

    assigned_main_ids = [
        coach_id
        for coach_id in using_ids
        if coach_id in main_ids and coach_id not in contractor_ids
    ]
    contractor_only = bool(using_ids) and all(
        coach_id in contractor_ids for coach_id in using_ids
    )
    burden_target_ids = main_ids if contractor_only else assigned_main_ids
    if not amount or not burden_target_ids:
        return {
            "burden_by_coach": {},
            "burden_target_ids": burden_target_ids,
            "rule": "contractor_only" if contractor_only else "assigned_main",
        }

    base, remainder = divmod(amount, len(burden_target_ids))
    burden_by_coach = {
        coach_id: base + (1 if index < remainder else 0)
        for index, coach_id in enumerate(burden_target_ids)
    }
    if sum(burden_by_coach.values()) != amount:
        raise ValueError("court cost allocation must equal the occurrence total")

    return {
        "burden_by_coach": burden_by_coach,
        "burden_target_ids": burden_target_ids,
        "rule": "contractor_only" if contractor_only else "assigned_main",
    }
