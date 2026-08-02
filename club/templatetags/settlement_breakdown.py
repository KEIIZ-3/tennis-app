from datetime import date

from django import template
from django.contrib.auth import get_user_model

from club.models import CoachExpense, RainRefund

register = template.Library()


def _money(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _split_amount(amount, coach_ids):
    unique_ids = list(dict.fromkeys(coach_id for coach_id in coach_ids if coach_id))
    if not unique_ids:
        return {}

    total = max(_money(amount), 0)
    base, remainder = divmod(total, len(unique_ids))
    return {
        coach_id: base + (1 if index < remainder else 0)
        for index, coach_id in enumerate(unique_ids)
    }


def _display_name(user):
    if not user:
        return "-"
    try:
        return user.display_name()
    except Exception:
        return getattr(user, "username", "-") or "-"


def _date_label(value):
    if not value:
        return "日付不明"
    if isinstance(value, date):
        return value.strftime("%Y/%m/%d")
    text = str(value)
    try:
        return date.fromisoformat(text[:10]).strftime("%Y/%m/%d")
    except (TypeError, ValueError):
        return text[:10] or "日付不明"


@register.inclusion_tag("coach/_court_cost_breakdown.html")
def court_cost_breakdown(settlement, row):
    coach = row.get("coach") if isinstance(row, dict) else None
    coach_id = getattr(coach, "pk", None)
    if settlement is None or coach_id is None:
        return {
            "court_rows": [],
            "rain_rows": [],
            "court_total": 0,
            "rain_refunded_total": 0,
            "rain_pending_total": 0,
        }

    snapshot = dict(getattr(settlement, "calculation_snapshot", None) or {})
    court_policy = dict(snapshot.get("court_policy") or {})
    detail_rows = list(court_policy.get("detail_rows") or [])

    expense_ids = {
        _money(detail.get("expense_id"))
        for detail in detail_rows
        if _money(detail.get("expense_id")) > 0
    }
    expense_map = {
        expense.pk: expense
        for expense in CoachExpense.objects.filter(pk__in=expense_ids)
    }

    user_ids = set()
    for detail in detail_rows:
        payer_id = _money(detail.get("payer_id"))
        if payer_id:
            user_ids.add(payer_id)
    user_map = {
        user.pk: user
        for user in get_user_model().objects.filter(pk__in=user_ids)
    }

    court_rows = []
    for detail in detail_rows:
        target_ids = [
            _money(value)
            for value in detail.get("burden_target_ids") or []
            if _money(value) > 0
        ]
        if coach_id not in target_ids:
            continue

        total_amount = _money(
            detail.get("amount")
            if detail.get("is_court_transfer")
            else detail.get("finalized_cost")
        )
        if total_amount <= 0:
            continue

        allocations = _split_amount(total_amount, target_ids)
        own_amount = _money(allocations.get(coach_id))
        if own_amount <= 0:
            continue

        expense_id = _money(detail.get("expense_id"))
        expense = expense_map.get(expense_id)
        expense_date = getattr(expense, "expense_date", None)
        start_at = detail.get("start_at")
        payer = user_map.get(_money(detail.get("payer_id")))

        court_rows.append(
            {
                "date_label": _date_label(expense_date or start_at),
                "lesson_label": (
                    "登録済みコート代"
                    if detail.get("is_court_transfer")
                    else "実施済みレッスン"
                ),
                "burden_rule": detail.get("burden_rule") or "担当コーチ負担",
                "total_amount": total_amount,
                "own_amount": own_amount,
                "payer_name": _display_name(payer),
            }
        )

    court_rows.sort(key=lambda item: item["date_label"])

    rain_rows = []
    rain_refunded_total = 0
    rain_pending_total = 0
    refunds = (
        RainRefund.objects.filter(
            lesson_date__year=settlement.year,
            lesson_date__month=settlement.month,
        )
        .select_related("debit_coach", "payer_coach", "collection_coach")
        .order_by("lesson_date", "id")
    )

    for refund in refunds:
        amount = max(_money(refund.amount), 0)
        if amount <= 0:
            continue

        is_pending = refund.status == RainRefund.STATUS_PENDING
        status_label = "返金予定・精算未反映" if is_pending else "返金済み・精算反映済み"

        if refund.payer_coach_id == coach_id:
            rain_rows.append(
                {
                    "date_label": _date_label(refund.lesson_date),
                    "lesson_label": refund.lesson_label or "雨天中止レッスン",
                    "direction": "plus",
                    "direction_label": "コート代の戻り",
                    "amount": amount,
                    "status_label": status_label,
                    "is_pending": is_pending,
                    "counterparty_name": _display_name(refund.debit_coach),
                }
            )
            if is_pending:
                rain_pending_total += amount
            else:
                rain_refunded_total += amount

        if refund.debit_coach_id == coach_id and refund.debit_coach_id != refund.payer_coach_id:
            rain_rows.append(
                {
                    "date_label": _date_label(refund.lesson_date),
                    "lesson_label": refund.lesson_label or "雨天中止レッスン",
                    "direction": "minus",
                    "direction_label": "返金分の負担",
                    "amount": amount,
                    "status_label": status_label,
                    "is_pending": is_pending,
                    "counterparty_name": _display_name(refund.payer_coach),
                }
            )

    return {
        "court_rows": court_rows,
        "rain_rows": rain_rows,
        "court_total": sum(item["own_amount"] for item in court_rows),
        "rain_refunded_total": rain_refunded_total,
        "rain_pending_total": rain_pending_total,
    }
