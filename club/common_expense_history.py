from collections import defaultdict
from datetime import date

from django.db.models import Min

from .models import CoachExpense


JULY_HISTORY_YEAR = 2026
JULY_HISTORY_MONTH = 7


def _next_month(year, month):
    if month == 12:
        return year + 1, 1
    return year, month + 1


def _month_sequence(start_year, start_month, end_year, end_month):
    year, month = int(start_year), int(start_month)
    end = (int(end_year), int(end_month))
    while (year, month) <= end:
        yield year, month
        year, month = _next_month(year, month)


def _add_amounts(target, source):
    for coach_id, amount in (source or {}).items():
        try:
            normalized_coach_id = int(coach_id)
        except (TypeError, ValueError):
            normalized_coach_id = coach_id
        target[normalized_coach_id] += int(amount or 0)


def _merge_policy_rows(policies):
    burden_by_coach = defaultdict(int)
    ball_burden_by_coach = defaultdict(int)
    other_burden_by_coach = defaultdict(int)
    ball_reimbursement_by_coach = defaultdict(int)
    other_reimbursement_by_coach = defaultdict(int)
    reimbursement_by_coach = defaultdict(int)
    detail_rows = []

    for source_year, source_month, policy in policies:
        _add_amounts(burden_by_coach, policy.get("burden_by_coach"))
        _add_amounts(ball_burden_by_coach, policy.get("ball_burden_by_coach"))
        _add_amounts(other_burden_by_coach, policy.get("other_burden_by_coach"))
        _add_amounts(
            ball_reimbursement_by_coach,
            policy.get("ball_reimbursement_by_coach"),
        )
        _add_amounts(
            other_reimbursement_by_coach,
            policy.get("other_reimbursement_by_coach"),
        )
        _add_amounts(reimbursement_by_coach, policy.get("reimbursement_by_coach"))

        for row in policy.get("detail_rows") or []:
            detail_rows.append(
                {
                    **dict(row),
                    "source_year": source_year,
                    "source_month": source_month,
                    "is_july_history": (
                        source_year != JULY_HISTORY_YEAR
                        or source_month != JULY_HISTORY_MONTH
                    ),
                }
            )

    return {
        "burden_by_coach": dict(burden_by_coach),
        "ball_burden_by_coach": dict(ball_burden_by_coach),
        "other_burden_by_coach": dict(other_burden_by_coach),
        "ball_reimbursement_by_coach": dict(ball_reimbursement_by_coach),
        "other_reimbursement_by_coach": dict(other_reimbursement_by_coach),
        "reimbursement_by_coach": dict(reimbursement_by_coach),
        "detail_rows": detail_rows,
        "expense_total": sum(int(row.get("amount") or 0) for row in detail_rows),
        "reimbursement_total": sum(reimbursement_by_coach.values()),
        "includes_history_through": "2026-07",
    }


def build_common_expense_policy(
    *,
    year,
    month,
    main_coach_ids,
    participant_count_by_coach,
    build_month_policy,
):
    """
    2026年7月だけ、登録開始月から7月までの共通経費をまとめて精算する。

    2026年8月以降は従来どおり対象月単月だけを返す。コート代・個人経費・
    給与支払等の除外判定は既存の build_month_policy に委譲し、既存仕様を保つ。
    """
    year = int(year)
    month = int(month)

    if (year, month) != (JULY_HISTORY_YEAR, JULY_HISTORY_MONTH):
        return build_month_policy(
            year,
            month,
            main_coach_ids,
            participant_count_by_coach,
        )

    earliest_date = CoachExpense.objects.aggregate(
        earliest=Min("expense_date")
    ).get("earliest")
    if not isinstance(earliest_date, date):
        earliest_date = date(JULY_HISTORY_YEAR, JULY_HISTORY_MONTH, 1)

    start_year, start_month = earliest_date.year, earliest_date.month
    if (start_year, start_month) > (JULY_HISTORY_YEAR, JULY_HISTORY_MONTH):
        start_year, start_month = JULY_HISTORY_YEAR, JULY_HISTORY_MONTH

    policies = []
    for source_year, source_month in _month_sequence(
        start_year,
        start_month,
        JULY_HISTORY_YEAR,
        JULY_HISTORY_MONTH,
    ):
        policy = build_month_policy(
            source_year,
            source_month,
            main_coach_ids,
            participant_count_by_coach,
        )
        policies.append((source_year, source_month, policy))

    return _merge_policy_rows(policies)
