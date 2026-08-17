"""Canonical selection helpers for availability-linked court transfers."""

from .expense_metadata import parse_expense_note
from .models import CoachExpense


COURT_TRANSFER_RECORD_KIND = "court_transfer"


def court_transfer_availability_id(expense):
    """Return the structured availability id, or ``None`` for legacy rows."""
    meta = parse_expense_note(expense.note)
    if meta.get("record_kind") != COURT_TRANSFER_RECORD_KIND:
        return None
    try:
        return int(meta.get("availability_id"))
    except (TypeError, ValueError):
        return None


def current_court_transfer_from_expenses(expenses, availability_id):
    """Select the latest-PK transfer for an availability without mutating rows."""
    target_id = int(availability_id)
    matches = [
        expense
        for expense in expenses
        if court_transfer_availability_id(expense) == target_id
    ]
    return max(matches, key=lambda expense: expense.pk, default=None)


def get_current_court_transfer_for_availability(
    availability_id,
    *,
    for_update=False,
):
    """Return the canonical current transfer (latest PK) for an availability."""
    expenses = CoachExpense.objects.filter(
        category=CoachExpense.CATEGORY_COURT,
    ).order_by("-id")
    if for_update:
        expenses = expenses.select_for_update()
    for expense in expenses:
        if court_transfer_availability_id(expense) == int(availability_id):
            return expense
    return None


def current_court_transfer_rows(expense_rows):
    """Deduplicate transfers by the real lesson occurrence, not row identity.

    Replacement availabilities may retain their historical transfer.  When both
    availabilities are linked to the same fixed lesson occurrence, the latest
    expense is canonical and the older expense remains as audit evidence.
    """
    from .models import CoachAvailability, Reservation

    availability_ids = set()
    for row in expense_rows:
        if row["meta"].get("record_kind") != COURT_TRANSFER_RECORD_KIND:
            continue
        try:
            availability_ids.add(int(row["meta"].get("availability_id")))
        except (TypeError, ValueError):
            pass

    availabilities = {
        item.pk: item
        for item in CoachAvailability.objects.filter(pk__in=availability_ids)
    }
    fixed_ids_by_availability = {}
    for availability_id, fixed_lesson_id in Reservation.objects.filter(
        availability_id__in=availability_ids,
        fixed_lesson_id__isnull=False,
    ).values_list("availability_id", "fixed_lesson_id").distinct():
        fixed_ids_by_availability.setdefault(availability_id, set()).add(
            fixed_lesson_id
        )

    occurrence_key_by_availability = {}
    for availability_id, availability in availabilities.items():
        fixed_ids = fixed_ids_by_availability.get(availability_id, set())
        if len(fixed_ids) == 1:
            start_at = availability.start_at
            end_at = availability.end_at
            occurrence_key_by_availability[availability_id] = (
                f"fixed:{next(iter(fixed_ids))}:"
                f"{start_at.date().isoformat()}:"
                f"{start_at.time().isoformat()}:{end_at.time().isoformat()}"
            )
        else:
            occurrence_key_by_availability[availability_id] = (
                f"availability:{availability_id}"
            )

    current_by_occurrence = {}
    rows_without_availability = []
    for row in expense_rows:
        expense = row["expense"]
        meta = row["meta"]
        if meta.get("record_kind") != COURT_TRANSFER_RECORD_KIND:
            continue
        try:
            availability_id = int(meta.get("availability_id"))
        except (TypeError, ValueError):
            rows_without_availability.append(row)
            continue
        occurrence_key = occurrence_key_by_availability.get(
            availability_id, f"availability:{availability_id}"
        )
        current = current_by_occurrence.get(occurrence_key)
        if current is None or expense.pk > current["expense"].pk:
            current_by_occurrence[occurrence_key] = row
    return (
        rows_without_availability,
        current_by_occurrence,
        occurrence_key_by_availability,
    )
