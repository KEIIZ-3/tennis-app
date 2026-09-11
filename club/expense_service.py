from datetime import date

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .models import CoachExpense, ensure_accounting_month_is_open
from .settlement_models import MonthlySettlement
from .settlement_service import calculate_monthly_settlement
from .expense_metadata import parse_expense_note


def _next_month(month_start):
    if month_start.month == 12:
        return date(month_start.year + 1, 1, 1)
    return date(month_start.year, month_start.month + 1, 1)


def _closed_month_message(month_start, *, current=False):
    label = f"{month_start.year}年{month_start.month}月"
    if current:
        return f"現在の適用月{label}は締め済みのため変更できません。"
    return f"{label}は締め済みのため、適用月に指定できません。"


def _expense_accounting_months(expense):
    months = {expense.expense_date.replace(day=1)} if expense.expense_date else set()
    if expense.category == CoachExpense.CATEGORY_BALL and expense.settlement_period_start:
        months.add(expense.settlement_period_start.replace(day=1))
    return months


def validate_expense_update(*, current, candidate):
    """Validate both sides of an accounting-significant expense edit."""
    candidate.full_clean()
    if not current:
        return
    significant_fields = (
        "expense_date", "category", "amount", "note", "settlement_period_start",
        "settlement_period_end", "created_by_id",
    )
    if not any(getattr(current, name) != getattr(candidate, name) for name in significant_fields):
        return
    for month in sorted(_expense_accounting_months(current) | _expense_accounting_months(candidate)):
        try:
            ensure_accounting_month_is_open(month)
        except ValidationError as exc:
            raise ValidationError(f"{month.year}年{month.month}月は締め済みのため変更できません。") from exc
    old_type = parse_expense_note(current.note).get("expense_type")
    new_type = parse_expense_note(candidate.note).get("expense_type")
    if old_type != new_type and old_type == "court_transfer":
        raise ValidationError("コート代振替の経費区分は変更できません。")


@transaction.atomic
def save_expense_update(*, candidate):
    current = None
    if candidate.pk:
        current = CoachExpense.objects.select_for_update().get(pk=candidate.pk)
    validate_expense_update(current=current, candidate=candidate)
    candidate.save()
    for month in sorted(_expense_accounting_months(current) | _expense_accounting_months(candidate) if current else _expense_accounting_months(candidate)):
        calculate_monthly_settlement(month.year, month.month, force=True)
    return candidate


@transaction.atomic
def update_ball_expense_application_month(*, expense, application_month, user):
    """Adminによるボール代の適用月だけを、安全に更新する。"""
    if not (
        getattr(user, "is_staff", False)
        or getattr(user, "is_superuser", False)
    ):
        raise PermissionDenied("ボール代の適用月を変更する権限がありません。")

    locked_expense = CoachExpense.objects.select_for_update().get(pk=expense.pk)
    if locked_expense.category != CoachExpense.CATEGORY_BALL:
        raise ValidationError("ボール代以外の適用月は変更できません。")

    old_month = locked_expense.settlement_period_start
    if old_month is None:
        raise ValidationError("現在の適用月が設定されていません。")
    old_month = old_month.replace(day=1)
    new_month = application_month.replace(day=1)
    if old_month == new_month:
        return locked_expense

    try:
        ensure_accounting_month_is_open(old_month)
    except ValidationError as exc:
        raise ValidationError(_closed_month_message(old_month, current=True)) from exc
    try:
        ensure_accounting_month_is_open(new_month)
    except ValidationError as exc:
        raise ValidationError(_closed_month_message(new_month)) from exc

    # 通常の save() は変更していない expense_date の締め状態も検証するため、
    # 明示検証済みの適用月2列だけをService内から更新する。
    CoachExpense.objects.filter(pk=locked_expense.pk).update(
        settlement_period_start=new_month,
        settlement_period_end=new_month,
    )
    locked_expense.settlement_period_start = new_month
    locked_expense.settlement_period_end = new_month

    month = min(old_month, new_month)
    last_month = max(old_month, new_month)
    while month <= last_month:
        is_closed = MonthlySettlement.objects.filter(
            year=month.year,
            month=month.month,
            status=MonthlySettlement.STATUS_CLOSED,
        ).exists()
        if not is_closed:
            calculate_monthly_settlement(month.year, month.month, force=True)
        month = _next_month(month)

    return locked_expense
