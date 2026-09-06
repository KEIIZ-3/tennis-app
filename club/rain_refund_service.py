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
def update_pending_rain_refund(availability_id, *, refund_input):
    """Update only the editable settlement parties for one pending rain refund."""
    refunds = list(
        RainRefund.objects.select_for_update()
        .select_related("expense")
        .filter(availability_id=availability_id)
    )
    if not refunds:
        raise ValidationError("返金待ちの雨天中止精算情報が見つかりません。")
    if len(refunds) != 1:
        raise ValidationError("対象開催回の雨天中止精算情報を一意に特定できません。")
    refund = refunds[0]
    if refund.status != RainRefund.STATUS_PENDING:
        raise ValidationError("返金待ちの雨天中止精算情報だけを修正できます。")

    ensure_accounting_month_is_open(refund.lesson_date)
    expense = CoachExpense.objects.select_for_update().get(pk=refund.expense_id)
    meta = parse_expense_note(expense.note)
    if meta.get("approval_status") != "refund_pending":
        raise ValidationError("返金待ちのコート代精算情報だけを修正できます。")

    account_coach = refund_input["account_coach"]
    collection_coach = refund_input["collection_coach"]
    payer_coach = refund_input["payer_coach"]
    debit_coach = refund_input["debit_coach"]
    account_name = (
        _display_name(account_coach)
        if account_coach
        else refund_input["account_other"]
    )
    meta.update(
        {
            "rain_refund_account_kind": refund_input["account_kind"],
            "rain_refund_account_coach_id": (
                account_coach.pk if account_coach else None
            ),
            "rain_refund_account_name": account_name,
            "rain_refund_account_other": refund_input["account_other"],
            "rain_refund_collection_coach_id": collection_coach.pk,
            "rain_refund_collection_coach_name": _display_name(collection_coach),
            "rain_refund_payer_coach_id": payer_coach.pk,
            "rain_refund_payer_coach_name": _display_name(payer_coach),
            "rain_refund_debit_coach_id": debit_coach.pk,
            "rain_refund_debit_coach_name": _display_name(debit_coach),
        }
    )
    expense.note = build_expense_note(meta, meta.get("plain_note", ""))
    expense.created_by = payer_coach
    expense.full_clean()
    expense.save(update_fields=["note", "created_by"])

    refund.booking_account_kind = refund_input["account_kind"]
    refund.booking_account_coach = account_coach
    refund.booking_account_other = refund_input["account_other"]
    refund.collection_coach = collection_coach
    refund.debit_coach = debit_coach
    refund.payer_coach = payer_coach
    refund.full_clean()
    refund.save(
        update_fields=[
            "booking_account_kind",
            "booking_account_coach",
            "booking_account_other",
            "collection_coach",
            "debit_coach",
            "payer_coach",
            "updated_at",
        ]
    )
    return refund


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
    already_refunded = refund.status == RainRefund.STATUS_REFUNDED
    if not already_refunded and refund.status != RainRefund.STATUS_PENDING:
        raise ValidationError("返金待ちの雨天中止返金だけを返金済みにできます。")

    if not already_refunded:
        ensure_accounting_month_is_open(refund.lesson_date)
    expense = CoachExpense.objects.select_for_update().get(pk=refund.expense_id)

    confirmed_at = refund.confirmed_at or timezone.now()
    confirmation_user = refund.confirmed_by if already_refunded else confirmed_by
    meta = parse_expense_note(expense.note)
    meta.update(
        {
            "approval_status": "refunded",
            "court_refunded_at": confirmed_at.isoformat(),
            "court_refunded_by_id": getattr(confirmation_user, "pk", None),
            "court_refunded_by_name": _display_name(confirmation_user),
        }
    )
    expense.note = build_expense_note(meta, meta.get("plain_note", ""))
    expense.save(update_fields=["note"])

    refund.status = RainRefund.STATUS_REFUNDED
    refund.confirmed_at = confirmed_at
    refund.confirmed_by = confirmation_user
    refund.save(
        update_fields=["status", "confirmed_at", "confirmed_by", "updated_at"]
    )
    return refund
