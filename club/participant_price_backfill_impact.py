"""Read-only impact analysis for recoverable legacy participant prices."""

from copy import copy
from datetime import date

from django.db.models import Prefetch
from django.utils import timezone

from .ball_expense_allocation import held_participant_count_by_coach
from .models import CoachExpense, Reservation, TicketConsumption
from .participant_price_integrity_diagnostic import recoverable_participant_price
from .settlement_balance_policy import (
    _approved_monthly_expenses,
    _execution_slot_key,
    _monthly_execution_reservations_and_status,
    _reservation_coaches,
    _split_amount_by_lesson_count,
    main_coaches,
)
from .settlement_models import MonthlySettlement


def _month_end(year, month):
    return date(year + (month == 12), 1 if month == 12 else month + 1, 1)


def _sorted_counts(counts, coach_ids):
    return {str(coach_id): int(counts.get(coach_id, 0)) for coach_id in coach_ids}


def _candidate_reservations():
    consumptions = TicketConsumption.objects.only(
        "id", "reservation_id", "tickets_used", "unit_price_snapshot", "refunded_at"
    ).order_by("id")
    return list(
        Reservation.objects.filter(participant_ticket_price_snapshot__isnull=True)
        .prefetch_related(
            Prefetch("ticket_consumptions", queryset=consumptions, to_attr="impact_consumptions")
        )
        .select_related(
            "coach", "substitute_coach", "availability", "fixed_lesson",
            "fixed_lesson__coach", "fixed_lesson__coach_2", "fixed_lesson__coach_3",
        )
        .order_by("id")
    )


def _counts(reservations, status_map, coach_ids):
    return held_participant_count_by_coach(
        reservations,
        status_map,
        eligible_coach_ids=coach_ids,
        execution_slot_key=_execution_slot_key,
        reservation_coaches=_reservation_coaches,
    )


def _ball_allocations(expense_rows, coach_ids, counts):
    rows = []
    for row in expense_rows:
        expense = row["expense"]
        if expense.category != CoachExpense.CATEGORY_BALL or row["is_court"]:
            continue
        allocation = _split_amount_by_lesson_count(
            row["amount"], coach_ids, counts
        )
        rows.append({
            "expense_id": expense.pk,
            "amount": int(row["amount"]),
            "payer_id": row["payer_id"],
            "allocation": _sorted_counts(allocation, coach_ids),
            "allocation_total": sum(allocation.values()),
        })
    return sorted(rows, key=lambda row: row["expense_id"])


def diagnose_participant_price_backfill_impact():
    """Compare current and virtual class-A snapshots without database writes."""
    candidates = []
    for reservation in _candidate_reservations():
        price = recoverable_participant_price(
            reservation, reservation.impact_consumptions
        )
        if price is not None:
            candidates.append((reservation, price))

    months = sorted({
        (timezone.localtime(reservation.start_at).year,
         timezone.localtime(reservation.start_at).month)
        for reservation, _price in candidates
    })
    main_coach_ids = sorted(coach.pk for coach in main_coaches())
    reservation_rows = []
    monthly_rows = []

    for year, month in months:
        settlement = MonthlySettlement.objects.filter(year=year, month=month).first()
        reservations, status_map = _monthly_execution_reservations_and_status(year, month)
        prices = {
            reservation.pk: price for reservation, price in candidates
            if timezone.localtime(reservation.start_at).year == year
            and timezone.localtime(reservation.start_at).month == month
        }
        before_counts = _counts(reservations, status_map, main_coach_ids)
        virtual_reservations = []
        for reservation in reservations:
            virtual = copy(reservation)
            if reservation.pk in prices:
                virtual.participant_ticket_price_snapshot = prices[reservation.pk]
            virtual_reservations.append(virtual)
        after_counts = _counts(virtual_reservations, status_map, main_coach_ids)
        expense_rows = _approved_monthly_expenses(
            date(year, month, 1), _month_end(year, month)
        )
        before_allocations = _ball_allocations(expense_rows, main_coach_ids, before_counts)
        after_allocations = _ball_allocations(expense_rows, main_coach_ids, after_counts)
        allocations = []
        for before, after in zip(before_allocations, after_allocations):
            differences = {
                key: after["allocation"][key] - before["allocation"][key]
                for key in before["allocation"]
            }
            allocations.append({
                "expense_id": before["expense_id"], "amount": before["amount"],
                "payer_id": before["payer_id"],
                "before_allocation": before["allocation"],
                "after_allocation": after["allocation"],
                "difference_by_coach": differences,
                "before_total": before["allocation_total"],
                "after_total": after["allocation_total"],
                "changed": any(differences.values()),
            })
        status = "not_exists" if settlement is None else (
            "closed" if settlement.status == MonthlySettlement.STATUS_CLOSED else "open"
        )
        count_changed = before_counts != after_counts
        allocation_changed = any(row["changed"] for row in allocations)
        monthly_rows.append({
            "year": year, "month": month, "settlement_id": getattr(settlement, "pk", None),
            "settlement_status": status,
            "affected_reservation_count": len(prices),
            "before_counts": _sorted_counts(before_counts, main_coach_ids),
            "after_counts": _sorted_counts(after_counts, main_coach_ids),
            "count_difference": {
                str(coach_id): after_counts.get(coach_id, 0) - before_counts.get(coach_id, 0)
                for coach_id in main_coach_ids
            },
            "total_before": sum(before_counts.values()),
            "total_after": sum(after_counts.values()),
            "zero_participants_before": sum(before_counts.values()) == 0,
            "zero_participants_after": sum(after_counts.values()) == 0,
            "count_changed": count_changed,
            "ball_expenses": allocations,
            "ball_expense_allocation_changed": allocation_changed,
        })
        reservation_by_id = {reservation.pk: reservation for reservation in reservations}
        for candidate, price in candidates:
            if candidate.pk not in prices:
                continue
            current = reservation_by_id.get(candidate.pk)
            slot_key = _execution_slot_key(current) if current else ""
            execution_status = (status_map.get(slot_key) or {}).get("status", "scheduled")
            is_held = bool(current and execution_status == "held")
            coaches = sorted(coach.pk for coach in _reservation_coaches(current or candidate))
            before_eligible = True
            after_eligible = price > 1000
            participant_count_changes = (
                is_held
                and before_eligible != after_eligible
                and any(coach_id in main_coach_ids for coach_id in coaches)
            )
            reservation_rows.append({
                "reservation_id": candidate.pk, "recovered_price": int(price),
                "price_band": "gt_1000" if price > 1000 else "lte_1000",
                "eligible_before": before_eligible, "eligible_after": after_eligible,
                "eligibility_changes": before_eligible != after_eligible,
                "year": year, "month": month, "reservation_status": candidate.status,
                "lesson_execution_status": execution_status,
                "is_ball_expense_participant": is_held and before_eligible,
                "effective_coach_ids": coaches, "settlement_status": status,
                "ball_participant_count_changes": participant_count_changes,
                "ball_allocation_changes": participant_count_changes and allocation_changed,
            })

    lte_count = sum(price <= 1000 for _reservation, price in candidates)
    return {
        "recoverable_count": len(candidates),
        "price_distribution": {"lte_1000": lte_count, "gt_1000": len(candidates) - lte_count},
        "reservations": sorted(reservation_rows, key=lambda row: row["reservation_id"]),
        "monthly_impact": monthly_rows,
        "backfill_effect_summary": {
            "eligibility_changes": sum(row["eligibility_changes"] for row in reservation_rows),
            "months_with_participant_count_changes": sum(row["count_changed"] for row in monthly_rows),
            "months_with_ball_allocation_changes": sum(
                row["ball_expense_allocation_changed"] for row in monthly_rows
            ),
        },
    }
