from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone

from .expense_metadata import build_expense_note, parse_expense_note
from .models import CoachExpense, RainRefund, ensure_accounting_month_is_open


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
    refund = (
        RainRefund.objects.select_for_update()
        .filter(expense_id=expense_id)
        .first()
    )
    if refund is None:
        return None
    if refund.status == RainRefund.STATUS_REFUNDED:
        return refund
    if refund.status != RainRefund.STATUS_PENDING:
        raise ValidationError("返金待ちの雨天中止返金だけを返金済みにできます。")

    ensure_accounting_month_is_open(refund.lesson_date)
    expense = CoachExpense.objects.select_for_update().get(pk=refund.expense_id)

    confirmed_at = timezone.now()
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
