from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .models import CoachAvailability, CoachExpense, Reservation
from .settlement_models import MonthlySettlement
from .settlement_service import calculate_monthly_settlement, get_or_create_monthly_settlement


STATUS_UNCONFIRMED = "unconfirmed"
STATUS_SCHEDULED = "scheduled"
STATUS_HELD = "held"
STATUS_RAIN_CANCELED = "rain_canceled"
STATUS_REFUND_PENDING = "refund_pending"
STATUS_REFUNDED = "refunded"

STATUS_LABELS = {
    STATUS_UNCONFIRMED: "実施確認待ち",
    STATUS_SCHEDULED: "開催予定",
    STATUS_HELD: "実施済み",
    STATUS_RAIN_CANCELED: "雨天中止",
    STATUS_REFUND_PENDING: "返金待ち",
    STATUS_REFUNDED: "返金済み",
}

STATUS_CLASSES = {
    STATUS_UNCONFIRMED: "pending",
    STATUS_SCHEDULED: "scheduled",
    STATUS_HELD: "held",
    STATUS_RAIN_CANCELED: "canceled",
    STATUS_REFUND_PENDING: "refund-pending",
    STATUS_REFUNDED: "refunded",
}

SNAPSHOT_KEY = "lesson_execution_statuses"


def _is_allowed(user):
    return bool(
        getattr(user, "is_staff", False)
        or getattr(user, "is_superuser", False)
        or str(getattr(user, "role", "") or "") in ("coach", "contractor_coach")
    )


def _month_range(year, month):
    start = date(int(year), int(month), 1)
    if int(month) == 12:
        end = date(int(year) + 1, 1, 1)
    else:
        end = date(int(year), int(month) + 1, 1)
    return start, end


def _previous_month(year, month):
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _next_month(year, month):
    if month == 12:
        return year + 1, 1
    return year, month + 1


def _month_url(year, month):
    return f"{reverse('club:lesson_execution_manage')}?year={int(year)}&month={int(month)}"


def _availability_key(availability):
    return f"availability:{availability.pk}"


def _read_status_map(settlement):
    snapshot = dict(settlement.calculation_snapshot or {})
    raw = snapshot.get(SNAPSHOT_KEY) or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _save_status(settlement, availability, status, user):
    snapshot = dict(settlement.calculation_snapshot or {})
    status_map = _read_status_map(settlement)
    status_map[_availability_key(availability)] = {
        "status": status,
        "updated_at": timezone.now().isoformat(),
        "updated_by_id": getattr(user, "pk", None),
        "updated_by_name": _display_name(user),
    }
    snapshot[SNAPSHOT_KEY] = status_map
    settlement.calculation_snapshot = snapshot
    settlement.updated_at = timezone.now()
    settlement.save(update_fields=["calculation_snapshot", "updated_at"])


def _display_name(user):
    if not user:
        return "-"
    try:
        return user.display_name()
    except Exception:
        return str(user)


def _local(value):
    if value and timezone.is_aware(value):
        return timezone.localtime(value)
    return value


def _reservation_queryset(availability):
    return Reservation.objects.filter(
        availability=availability,
        start_at=availability.start_at,
        end_at=availability.end_at,
    ).select_related("user", "coach", "substitute_coach", "fixed_lesson")


def _coach_names(availability, reservations):
    names = []

    for reservation in reservations:
        fixed_lesson = getattr(reservation, "fixed_lesson", None)
        if fixed_lesson:
            try:
                for coach in fixed_lesson.all_coaches():
                    coach_name = _display_name(coach)
                    if coach_name and coach_name not in names:
                        names.append(coach_name)
            except Exception:
                pass

        assigned = getattr(reservation, "substitute_coach", None) or getattr(reservation, "coach", None)
        assigned_name = _display_name(assigned)
        if assigned_name and assigned_name not in names:
            names.append(assigned_name)

    if not names:
        assigned = availability.substitute_coach or availability.coach
        names.append(_display_name(assigned))

    return "・".join(name for name in names if name and name != "-") or "-"


def _mark_refunded(availability, changed_by):
    from .views import (
        EXPENSE_APPROVAL_REFUNDED,
        EXPENSE_NOTE_META_PREFIX,
        _court_expense_matches_availability,
        _expense_build_note,
        _expense_parse_note,
    )

    changed_count = 0
    expenses = CoachExpense.objects.filter(
        expense_date=_local(availability.start_at).date(),
        category=CoachExpense.CATEGORY_COURT,
    ).order_by("id")

    for expense in expenses:
        if not _court_expense_matches_availability(expense, availability):
            continue

        meta = _expense_parse_note(expense.note)
        extra_meta = {
            key: value
            for key, value in meta.items()
            if key not in {
                "expense_type",
                "receipt_status",
                "receipt_check_status",
                "approval_status",
                "plain_note",
            }
        }
        extra_meta.update(
            {
                "court_refunded_at": timezone.now().isoformat(),
                "court_refunded_by_id": getattr(changed_by, "pk", None),
                "court_refunded_by_name": _display_name(changed_by),
            }
        )
        expense.note = _expense_build_note(
            meta.get("plain_note", ""),
            expense_type=meta.get("expense_type", "common"),
            receipt_status=meta.get("receipt_status", "none"),
            receipt_check_status=meta.get("receipt_check_status", "unchecked"),
            approval_status=EXPENSE_APPROVAL_REFUNDED,
            extra_meta=extra_meta,
        )
        if expense.note.startswith(EXPENSE_NOTE_META_PREFIX):
            expense.save(update_fields=["note"])
            changed_count += 1

    return changed_count


def _patch_settlement_court_eligibility():
    from . import settlement_balance_policy

    current = settlement_balance_policy._eligible_reservations
    if getattr(current, "_execution_status_filter_applied", False):
        return

    original = current

    def eligible_with_execution_status(year, month):
        reservations = original(year, month)
        settlement = MonthlySettlement.objects.filter(year=int(year), month=int(month)).first()
        if settlement is None:
            return []

        status_map = _read_status_map(settlement)
        eligible = []
        for reservation in reservations:
            availability_id = getattr(reservation, "availability_id", None)
            if not availability_id:
                continue
            entry = status_map.get(f"availability:{availability_id}") or {}
            if entry.get("status") == STATUS_HELD:
                eligible.append(reservation)
        return eligible

    eligible_with_execution_status._execution_status_filter_applied = True
    eligible_with_execution_status._original = original
    settlement_balance_policy._eligible_reservations = eligible_with_execution_status


_patch_settlement_court_eligibility()


@login_required
@require_http_methods(["GET", "POST"])
def lesson_execution_manage(request):
    if not _is_allowed(request.user):
        return HttpResponse("Forbidden", status=403)

    today = timezone.localdate()
    try:
        selected_year = int(request.GET.get("year") or request.POST.get("year") or today.year)
    except Exception:
        selected_year = today.year
    try:
        selected_month = int(request.GET.get("month") or request.POST.get("month") or today.month)
    except Exception:
        selected_month = today.month

    if selected_year < 2024 or selected_year > 2100:
        selected_year = today.year
    if selected_month < 1 or selected_month > 12:
        selected_month = today.month

    month_start, next_month = _month_range(selected_year, selected_month)
    redirect_url = _month_url(selected_year, selected_month)
    settlement = get_or_create_monthly_settlement(selected_year, selected_month)

    if request.method == "POST":
        if settlement.is_closed:
            messages.error(request, "締め済みの月は開催状態を変更できません。")
            return redirect(redirect_url)

        availability_id = (request.POST.get("availability_id") or "").strip()
        action = (request.POST.get("action") or "").strip()
        availability = get_object_or_404(
            CoachAvailability.objects.select_related("coach", "substitute_coach", "court"),
            pk=availability_id,
            start_at__date__gte=month_start,
            start_at__date__lt=next_month,
        )

        reservations = list(_reservation_queryset(availability))

        if action == STATUS_HELD:
            if availability.end_at > timezone.now():
                messages.error(request, "終了前のレッスンは実施済みにできません。")
                return redirect(redirect_url)
            if not any(reservation.status == Reservation.STATUS_ACTIVE for reservation in reservations):
                messages.error(request, "有効な予約がないため実施済みにできません。")
                return redirect(redirect_url)
            _save_status(settlement, availability, STATUS_HELD, request.user)
            messages.success(request, "レッスンを実施済みにしました。売上とコート代の精算対象になります。")

        elif action == STATUS_RAIN_CANCELED:
            canceled_count = 0
            with transaction.atomic():
                for reservation in reservations:
                    if reservation.status != Reservation.STATUS_ACTIVE:
                        continue
                    reservation.cancel(
                        created_by=request.user,
                        reason="雨天中止による自動返却",
                    )
                    canceled_count += 1

                from .views import _mark_court_expenses_refund_pending_for_rain_cancel

                pending_count = _mark_court_expenses_refund_pending_for_rain_cancel(
                    availability,
                    changed_by=request.user,
                )
                next_status = STATUS_REFUND_PENDING if pending_count > 0 else STATUS_RAIN_CANCELED
                _save_status(settlement, availability, next_status, request.user)

            messages.success(
                request,
                f"雨天中止を登録しました。予約{canceled_count}件をキャンセルし、チケットを返却しました。",
            )

        elif action == STATUS_REFUNDED:
            refunded_count = _mark_refunded(availability, request.user)
            _save_status(settlement, availability, STATUS_REFUNDED, request.user)
            messages.success(
                request,
                f"コート代の返金済みを登録しました。対象経費{refunded_count}件を精算対象外にしました。",
            )

        else:
            messages.error(request, "変更内容が正しくありません。")
            return redirect(redirect_url)

        calculate_monthly_settlement(selected_year, selected_month, force=True)
        return redirect(redirect_url)

    status_map = _read_status_map(settlement)
    availabilities = list(
        CoachAvailability.objects.filter(
            start_at__date__gte=month_start,
            start_at__date__lt=next_month,
        )
        .select_related("coach", "substitute_coach", "court")
        .order_by("start_at", "id")
    )

    rows = []
    counts = {
        STATUS_UNCONFIRMED: 0,
        STATUS_SCHEDULED: 0,
        STATUS_HELD: 0,
        STATUS_RAIN_CANCELED: 0,
        STATUS_REFUND_PENDING: 0,
        STATUS_REFUNDED: 0,
    }

    for availability in availabilities:
        reservations = list(_reservation_queryset(availability))
        entry = status_map.get(_availability_key(availability)) or {}
        saved_status = entry.get("status")

        if saved_status in STATUS_LABELS:
            status = saved_status
        elif availability.end_at > timezone.now():
            status = STATUS_SCHEDULED
        else:
            status = STATUS_UNCONFIRMED

        counts[status] = counts.get(status, 0) + 1
        active_count = sum(
            1 for reservation in reservations if reservation.status == Reservation.STATUS_ACTIVE
        )
        canceled_count = sum(
            1 for reservation in reservations if reservation.status == Reservation.STATUS_CANCELED
        )

        rows.append(
            {
                "availability": availability,
                "start_local": _local(availability.start_at),
                "end_local": _local(availability.end_at),
                "coach_names": _coach_names(availability, reservations),
                "status": status,
                "status_label": STATUS_LABELS[status],
                "status_class": STATUS_CLASSES[status],
                "active_count": active_count,
                "canceled_count": canceled_count,
                "can_mark_held": availability.end_at <= timezone.now() and active_count > 0,
                "can_mark_rain": status not in (STATUS_REFUNDED, STATUS_HELD),
                "can_mark_refunded": status in (STATUS_RAIN_CANCELED, STATUS_REFUND_PENDING),
                "updated_by_name": entry.get("updated_by_name", ""),
            }
        )

    prev_year, prev_month = _previous_month(selected_year, selected_month)
    next_year, next_month_value = _next_month(selected_year, selected_month)

    return render(
        request,
        "coach/lesson_execution_manage.html",
        {
            "rows": rows,
            "selected_year": selected_year,
            "selected_month": selected_month,
            "month_label": f"{selected_year}年{selected_month}月",
            "prev_url": _month_url(prev_year, prev_month),
            "next_url": _month_url(next_year, next_month_value),
            "settlement_url": (
                f"{reverse('club:coach_admin_settlement')}?"
                f"year={selected_year}&month={selected_month}"
            ),
            "is_month_closed": settlement.is_closed,
            "counts": counts,
        },
    )
