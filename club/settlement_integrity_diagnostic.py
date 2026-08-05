"""Read-only diagnostics for closed settlements affected by legacy ball allocation."""

from collections import defaultdict

from django.urls import reverse
from django.utils import timezone

from .settlement_balance_policy import (
    _approved_monthly_expenses,
    _execution_slot_key,
    _month_range,
    _money,
    _monthly_execution_reservations_and_status,
    _reservation_coaches,
    _split_amount_by_lesson_count,
    main_coaches,
)
from .settlement_models import CoachMonthlySettlement, MonthlySettlement


def _participant_counts(reservations, status_map, coach_ids, allowed_statuses):
    """Count unique participants per occurrence and assigned coach."""
    eligible_coach_ids = set(coach_ids)
    participants_by_slot = defaultdict(set)
    coach_ids_by_slot = defaultdict(set)

    for reservation in reservations:
        slot_key = _execution_slot_key(reservation)
        if not slot_key:
            continue
        status = (status_map.get(slot_key) or {}).get("status", "scheduled")
        if status not in allowed_statuses:
            continue
        participant_id = getattr(reservation, "user_id", None)
        if participant_id is None:
            participant_id = f"reservation:{reservation.pk}"
        participants_by_slot[slot_key].add(participant_id)
        coach_ids_by_slot[slot_key].update(
            coach.pk
            for coach in _reservation_coaches(reservation)
            if coach.pk in eligible_coach_ids
        )

    counts = defaultdict(int)
    for slot_key, participant_ids in participants_by_slot.items():
        for coach_id in coach_ids_by_slot[slot_key]:
            counts[coach_id] += len(participant_ids)
    return dict(counts)


def _ball_amount(year, month):
    month_start, next_month = _month_range(year, month)
    return sum(
        _money(row["amount"])
        for row in _approved_monthly_expenses(month_start, next_month)
        if not row["is_court"]
        and row["expense_type"] != "personal"
        and getattr(row["expense"], "category", "") == "ball"
    )


def diagnose_closed_settlement(settlement, coaches=None):
    """Return a comparison without saving or recalculating settlement models."""
    coaches = list(coaches if coaches is not None else main_coaches())
    coach_ids = [coach.pk for coach in coaches]
    reservations, status_map = _monthly_execution_reservations_and_status(
        settlement.year, settlement.month
    )
    old_counts = _participant_counts(
        reservations, status_map, coach_ids, {"held", "scheduled"}
    )
    current_counts = _participant_counts(
        reservations, status_map, coach_ids, {"held"}
    )
    amount = _ball_amount(settlement.year, settlement.month)
    old_burdens = _split_amount_by_lesson_count(amount, coach_ids, old_counts)
    current_burdens = _split_amount_by_lesson_count(
        amount, coach_ids, current_counts
    )
    saved_rows = {
        row.coach_id: row
        for row in CoachMonthlySettlement.objects.filter(
            monthly_settlement=settlement, coach_id__in=coach_ids
        ).select_related("coach")
    }

    scheduled_slots = {}
    for reservation in reservations:
        slot_key = _execution_slot_key(reservation)
        if not slot_key or (status_map.get(slot_key) or {}).get(
            "status", "scheduled"
        ) != "scheduled":
            continue
        scheduled_slots.setdefault(slot_key, reservation)

    coach_rows = []
    for coach in coaches:
        saved_row = saved_rows.get(coach.pk)
        snapshot = dict(getattr(saved_row, "calculation_snapshot", {}) or {})
        saved_burden = snapshot.get("ball_expense_burden")
        reference_burden = current_burdens.get(coach.pk, 0)
        coach_rows.append(
            {
                "coach_name": coach.display_name(),
                "old_count": old_counts.get(coach.pk, 0),
                "current_count": current_counts.get(coach.pk, 0),
                "saved_burden": saved_burden,
                "saved_burden_registered": saved_burden is not None,
                "reference_burden": reference_burden,
                "difference": (
                    reference_burden - _money(saved_burden)
                    if saved_burden is not None
                    else None
                ),
            }
        )

    occurrence_rows = []
    execution_url = reverse("club:lesson_execution_manage")
    for reservation in scheduled_slots.values():
        local_start = (
            timezone.localtime(reservation.start_at)
            if timezone.is_aware(reservation.start_at)
            else reservation.start_at
        )
        availability_id = getattr(reservation, "availability_id", None)
        occurrence_rows.append(
            {
                "label": local_start.strftime("%Y-%m-%d %H:%M"),
                "url": (
                    f"{execution_url}?year={settlement.year}&month={settlement.month}"
                    + (f"#lesson-{availability_id}" if availability_id else "")
                ),
            }
        )

    return {
        "year": settlement.year,
        "month": settlement.month,
        "month_label": f"{settlement.year}年{settlement.month}月",
        "scheduled_count": len(scheduled_slots),
        "occurrences": occurrence_rows,
        "coach_rows": coach_rows,
        "ball_amount": amount,
        "allocation_changed": old_burdens != current_burdens,
        "is_candidate": bool(scheduled_slots) and old_burdens != current_burdens,
    }


def affected_closed_settlements():
    """List only closed months whose legacy/current allocations differ."""
    coaches = main_coaches()
    results = [
        diagnose_closed_settlement(settlement, coaches)
        for settlement in MonthlySettlement.objects.filter(
            status=MonthlySettlement.STATUS_CLOSED
        ).order_by("-year", "-month")
    ]
    return [result for result in results if result["is_candidate"]]
