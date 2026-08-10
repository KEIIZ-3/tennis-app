from django.db import transaction
from django.utils import timezone

from .expense_metadata import build_expense_note, parse_expense_note
from .models import CoachExpense, RainRefund


def _display_name(user):
    if not user:
        return "-"
    try:
        return str(user.display_name() or "-")
    except Exception:
        return str(user)


@transaction.atomic
def confirm_rain_refund(expense_id, *, confirmed_by):
    """Mark one court refund as confirmed in both persisted representations."""
    expense = CoachExpense.objects.select_for_update().get(pk=expense_id)
    refund = RainRefund.objects.select_for_update().filter(expense=expense).first()
    if refund is None:
        return None

    confirmed_at = refund.confirmed_at or timezone.now()
    meta = parse_expense_note(expense.note)
    meta.update(
        {
            "approval_status": "refunded",
            "court_refunded_at": confirmed_at.isoformat(),
            "court_refunded_by_id": getattr(confirmed_by, "pk", None),
            "court_refunded_by_name": _display_name(confirmed_by),
        }
    )
    expense.note = build_expense_note(meta, meta.get("plain_note", ""))
    expense.save(update_fields=["note"])

    refund.status = RainRefund.STATUS_REFUNDED
    refund.confirmed_at = confirmed_at
    refund.confirmed_by = confirmed_by
    refund.save(
        update_fields=["status", "confirmed_at", "confirmed_by", "updated_at"]
    )
    return refund
