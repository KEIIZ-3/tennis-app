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
    """Deduplicate settlement loader rows with the canonical selection rule."""
    current_by_availability = {}
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
        current = current_by_availability.get(availability_id)
        if current is None or expense.pk > current["expense"].pk:
            current_by_availability[availability_id] = row
    return rows_without_availability, current_by_availability
