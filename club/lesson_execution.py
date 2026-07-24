from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .lesson_execution_storage import read_status_map, save_status
from .models import CoachAvailability, CoachExpense, Court, FixedLesson, Reservation
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


def _is_allowed(user):
    return bool(
        getattr(user, "is_staff", False)
        or getattr(user, "is_superuser", False)
        or str(getattr(user, "role", "") or "") in ("coach", "contractor_coach")
    )


def _user_can_manage_slot(user, slot):
    if getattr(user, "role", "") != "contractor_coach":
        return True
    fixed_lesson = slot.get("fixed_lesson")
    if fixed_lesson:
        try:
            return any(coach.pk == user.pk for coach in fixed_lesson.all_coaches())
        except Exception:
            return getattr(fixed_lesson, "coach_id", None) == user.pk
    availability = slot.get("availability")
    return bool(
        availability
        and (
            getattr(availability, "coach_id", None) == user.pk
            or getattr(availability, "substitute_coach_id", None) == user.pk
        )
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


def _month_url(year, month, pending_only=False):
    url = (
        f"{reverse('club:lesson_execution_manage')}"
        f"?year={int(year)}&month={int(month)}"
    )
    if pending_only:
        url += "&pending=1"
    return url


def _display_name(user):
    if not user:
        return "-"
    try:
        return str(user.display_name() or "-")
    except Exception:
        return str(user)


def _local(value):
    if value and timezone.is_aware(value):
        return timezone.localtime(value)
    return value


def status_by_availability(user, year_month_pairs):
    """受付・精算画面でも実施管理と同じ判定結果を表示する。"""
    result = {}
    for year, month in sorted(set(year_month_pairs)):
        settlement = get_or_create_monthly_settlement(year, month)
        status_map = read_status_map(settlement)
        month_start, next_month = _month_range(year, month)
        court_expenses = list(
            CoachExpense.objects.filter(
                expense_date__gte=month_start,
                expense_date__lt=next_month,
                category=CoachExpense.CATEGORY_COURT,
            )
            .select_related("created_by")
            .order_by("-id")
        )
        for slot in _canonical_slots(year, month):
            if not _user_can_manage_slot(user, slot):
                continue
            availability = slot["availability"]
            entry = _status_entry(status_map, slot)
            saved_status = entry.get("status")
            if saved_status in STATUS_LABELS:
                status = saved_status
            elif slot["end_at"] > timezone.now():
                status = STATUS_SCHEDULED
            else:
                status = STATUS_UNCONFIRMED

            court_expense, court_meta = _court_expense_for_availability(
                court_expenses,
                availability,
            )
            approval_status = court_meta.get("approval_status", "")
            court_not_required = bool(
                court_meta.get("court_cost_not_required")
            )
            court_registered = bool(
                court_expense is not None
                and approval_status == "approved"
            )
            if status in (STATUS_RAIN_CANCELED, STATUS_REFUND_PENDING):
                court_status = (
                    "refund_pending" if court_expense else "not_required"
                )
                court_status_label = (
                    "返金待ち" if court_expense else "登録不要"
                )
            elif status == STATUS_REFUNDED:
                court_status = "not_required"
                court_status_label = "返金済み"
            elif court_not_required:
                court_status = "not_required"
                court_status_label = "コート代なし"
            elif court_registered:
                court_status = "registered"
                court_status_label = "登録済み"
            elif status == STATUS_HELD:
                court_status = "unregistered"
                court_status_label = "未登録"
            elif status == STATUS_SCHEDULED:
                court_status = "scheduled"
                court_status_label = "開催後に確認"
            else:
                court_status = "waiting"
                court_status_label = "実施確認後"

            result[availability.pk] = {
                "execution_status": status,
                "execution_status_label": STATUS_LABELS[status],
                "court_status": court_status,
                "court_status_label": court_status_label,
                "court_amount": (
                    int(court_expense.amount or 0)
                    if court_expense is not None
                    else None
                ),
                "court_payer_name": (
                    _display_name(court_expense.created_by)
                    if court_expense is not None
                    and not court_not_required
                    else ""
                ),
                "execution_needs_attention": bool(
                    status in (STATUS_UNCONFIRMED, STATUS_REFUND_PENDING)
                    or court_status == "unregistered"
                ),
            }
    return result


def _availability_key(availability):
    return f"availability:{availability.pk}"


def _fixed_slot_key(fixed_lesson, target_date):
    return f"fixed:{fixed_lesson.pk}:{target_date.isoformat()}"


def _slot_key(slot):
    if slot.get("fixed_lesson") is not None:
        return _fixed_slot_key(slot["fixed_lesson"], slot["target_date"])
    return _availability_key(slot["availability"])


def _legacy_keys(slot):
    availability = slot.get("availability")
    return [_availability_key(availability)] if availability else []


def _fixed_coach_names(fixed_lesson):
    names = []
    try:
        coaches = fixed_lesson.all_coaches()
    except Exception:
        coaches = [
            getattr(fixed_lesson, "coach", None),
            getattr(fixed_lesson, "coach_2", None),
            getattr(fixed_lesson, "coach_3", None),
        ]

    for coach in coaches:
        coach_name = _display_name(coach)
        if coach_name and coach_name != "-" and coach_name not in names:
            names.append(coach_name)

    return "・".join(names) or "-"


def _availability_coach_names(availability):
    assigned = availability.substitute_coach or availability.coach
    return _display_name(assigned)


def _reservation_queryset(slot):
    availability = slot["availability"]
    fixed_lesson = slot.get("fixed_lesson")
    start_at = slot["start_at"]
    end_at = slot["end_at"]

    condition = Q(
        availability=availability,
        start_at=start_at,
        end_at=end_at,
    )

    if fixed_lesson is not None:
        condition |= Q(
            fixed_lesson=fixed_lesson,
            start_at=start_at,
            end_at=end_at,
        )

    return (
        Reservation.objects.filter(condition)
        .select_related(
            "user",
            "coach",
            "substitute_coach",
            "fixed_lesson",
            "availability",
        )
        .distinct()
        .order_by("id")
    )


def _canonical_availability_for_fixed(fixed_lesson, start_at, end_at):
    primary_coach = (
        fixed_lesson.primary_coach()
        if hasattr(fixed_lesson, "primary_coach")
        else fixed_lesson.coach
    )
    court = fixed_lesson.court or Court.objects.filter(
        is_active=True,
    ).order_by("id").first()
    if primary_coach is None or court is None:
        return None

    defaults = {
        "capacity": max(int(fixed_lesson.effective_capacity() or 1), 1),
        "coach_count": max(int(fixed_lesson.coach_count or 1), 1),
        "court_count": max(int(fixed_lesson.court_count or 1), 1),
        "target_level": fixed_lesson.target_level,
        "target_level_2": fixed_lesson.target_level_2 or "",
        "status": CoachAvailability.STATUS_OPEN,
        "note": (
            f"固定レッスン: "
            f"{fixed_lesson.title or fixed_lesson.get_weekday_display()}"
        ),
    }
    availability, _created = CoachAvailability.objects.get_or_create(
        coach=primary_coach,
        court=court,
        lesson_type=fixed_lesson.lesson_type,
        start_at=start_at,
        end_at=end_at,
        defaults=defaults,
    )

    update_fields = []
    for field_name, expected_value in defaults.items():
        if getattr(availability, field_name) != expected_value:
            setattr(availability, field_name, expected_value)
            update_fields.append(field_name)

    if update_fields:
        availability.save(update_fields=update_fields)

    return availability


def _canonical_slots(year, month):
    month_start, next_month = _month_range(year, month)
    slots = []
    represented_availability_ids = set()

    fixed_lessons = (
        FixedLesson.objects.filter(is_active=True)
        .select_related("coach", "coach_2", "coach_3", "court")
        .order_by("id")
    )

    for fixed_lesson in fixed_lessons:
        for target_date in fixed_lesson.scheduled_occurrence_dates():
            if not (month_start <= target_date < next_month):
                continue

            start_at, end_at = fixed_lesson._build_datetimes_for_date(target_date)
            availability = _canonical_availability_for_fixed(
                fixed_lesson,
                start_at,
                end_at,
            )
            if availability is None:
                continue

            represented_availability_ids.add(availability.pk)
            slots.append(
                {
                    "availability": availability,
                    "fixed_lesson": fixed_lesson,
                    "target_date": target_date,
                    "start_at": start_at,
                    "end_at": end_at,
                    "coach_names": _fixed_coach_names(fixed_lesson),
                    "source_kind": "fixed_lesson",
                }
            )

    extra_availabilities = (
        CoachAvailability.objects.filter(
            start_at__date__gte=month_start,
            start_at__date__lt=next_month,
        )
        .exclude(lesson_type=Reservation.LESSON_GENERAL)
        .exclude(pk__in=represented_availability_ids)
        .select_related("coach", "substitute_coach", "court")
        .order_by("start_at", "id")
    )

    for availability in extra_availabilities:
        slots.append(
            {
                "availability": availability,
                "fixed_lesson": None,
                "target_date": _local(availability.start_at).date(),
                "start_at": availability.start_at,
                "end_at": availability.end_at,
                "coach_names": _availability_coach_names(availability),
                "source_kind": "availability",
            }
        )

    slots.sort(key=lambda row: (row["start_at"], row["availability"].pk))
    return slots


def _status_entry(status_map, slot):
    entry = status_map.get(_slot_key(slot))
    if entry:
        return entry

    for legacy_key in _legacy_keys(slot):
        entry = status_map.get(legacy_key)
        if entry:
            return entry

    return {}


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
            if key
            not in {
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


def _court_expense_for_availability(expenses, availability):
    from .court_expense_transfer import _parse_note as _parse_transfer_note
    from .views import _court_expense_matches_availability, _expense_parse_note

    for expense in expenses:
        transfer_meta = _parse_transfer_note(expense.note)
        try:
            linked_availability_id = int(
                transfer_meta.get("availability_id")
            )
        except (TypeError, ValueError):
            linked_availability_id = None
        if linked_availability_id == availability.pk:
            return expense, transfer_meta
        if _court_expense_matches_availability(expense, availability):
            return expense, _expense_parse_note(expense.note)
    return None, {}


def _mark_court_cost_not_required(availability, changed_by):
    from .court_expense_transfer import (
        APPROVAL_APPROVED,
        RECORD_KIND,
        _build_note,
        _existing_transfer_for_availability,
        _facility_label,
        _lesson_label,
        _slot_key as _court_slot_key,
        _using_coaches,
    )

    using_coaches = _using_coaches(availability)
    meta = {
        "expense_type": "court_transfer",
        "receipt_status": "none",
        "receipt_check_status": "checked",
        "approval_status": APPROVAL_APPROVED,
        "record_kind": RECORD_KIND,
        "availability_id": availability.pk,
        "court_refund_slot_key": _court_slot_key(availability),
        "court_refund_lesson_label": _lesson_label(availability),
        "court_refund_facility_label": _facility_label(availability.court),
        "payer_coach_id": None,
        "payer_coach_name": "登録不要",
        "using_coach_ids": [coach.pk for coach in using_coaches],
        "using_coach_names": [_display_name(coach) for coach in using_coaches],
        "recorded_by_id": changed_by.pk,
        "recorded_by_name": _display_name(changed_by),
        "court_cost_not_required": True,
    }
    with transaction.atomic():
        CoachAvailability.objects.select_for_update().get(pk=availability.pk)
        expense = _existing_transfer_for_availability(availability.pk)
        if expense is not None and int(expense.amount or 0) > 0:
            return False
        if expense is None:
            expense = CoachExpense(category=CoachExpense.CATEGORY_COURT)
        expense.expense_date = _local(availability.start_at).date()
        expense.amount = 0
        expense.note = _build_note(meta, "コート代なし")
        expense.created_by = changed_by
        expense.full_clean()
        expense.save()
    return True


@login_required
@require_http_methods(["GET", "POST"])
def lesson_execution_manage(request):
    if not _is_allowed(request.user):
        return HttpResponse("Forbidden", status=403)

    today = timezone.localdate()
    try:
        selected_year = int(
            request.GET.get("year")
            or request.POST.get("year")
            or today.year
        )
    except Exception:
        selected_year = today.year

    try:
        selected_month = int(
            request.GET.get("month")
            or request.POST.get("month")
            or today.month
        )
    except Exception:
        selected_month = today.month

    if selected_year < 2024 or selected_year > 2100:
        selected_year = today.year
    if selected_month < 1 or selected_month > 12:
        selected_month = today.month

    pending_only = str(
        request.GET.get("pending")
        or request.POST.get("pending")
        or ""
    ).strip() == "1"
    redirect_url = _month_url(
        selected_year,
        selected_month,
        pending_only=pending_only,
    )
    settlement = get_or_create_monthly_settlement(
        selected_year,
        selected_month,
    )
    all_slots = _canonical_slots(selected_year, selected_month)
    all_slots_by_availability_id = {
        str(slot["availability"].pk): slot for slot in all_slots
    }
    slots = [
        slot for slot in all_slots
        if _user_can_manage_slot(request.user, slot)
    ]
    slots_by_availability_id = {
        str(slot["availability"].pk): slot for slot in slots
    }

    if request.method == "POST":
        if settlement.is_closed:
            messages.error(
                request,
                "締め済みの月は開催状態を変更できません。",
            )
            return redirect(redirect_url)

        availability_id = (
            request.POST.get("availability_id") or ""
        ).strip()
        action = (request.POST.get("action") or "").strip()
        if (
            availability_id in all_slots_by_availability_id
            and availability_id not in slots_by_availability_id
        ):
            return HttpResponse("Forbidden", status=403)
        slot = slots_by_availability_id.get(availability_id)

        if slot is None:
            messages.error(
                request,
                "対象レッスンは現在のレッスンカレンダーに存在しません。",
            )
            return redirect(redirect_url)

        availability = slot["availability"]
        reservations = list(_reservation_queryset(slot))

        if action == STATUS_HELD:
            if slot["end_at"] > timezone.now():
                messages.error(
                    request,
                    "終了前のレッスンは実施済みにできません。",
                )
                return redirect(redirect_url)

            if not any(
                reservation.status == Reservation.STATUS_ACTIVE
                for reservation in reservations
            ):
                messages.error(
                    request,
                    "有効な予約がないため実施済みにできません。",
                )
                return redirect(redirect_url)

            save_status(
                settlement,
                _slot_key(slot),
                STATUS_HELD,
                request.user,
                legacy_keys=_legacy_keys(slot),
            )
            messages.success(
                request,
                "レッスンを実施済みにしました。売上とコート代の精算対象になります。",
            )

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

                from .views import (
                    _mark_court_expenses_refund_pending_for_rain_cancel,
                )

                pending_count = (
                    _mark_court_expenses_refund_pending_for_rain_cancel(
                        availability,
                        changed_by=request.user,
                    )
                )
                next_status = (
                    STATUS_REFUND_PENDING
                    if pending_count > 0
                    else STATUS_RAIN_CANCELED
                )
                save_status(
                    settlement,
                    _slot_key(slot),
                    next_status,
                    request.user,
                    legacy_keys=_legacy_keys(slot),
                )

            messages.success(
                request,
                f"雨天中止を登録しました。予約{canceled_count}件をキャンセルし、チケットを返却しました。",
            )

        elif action == STATUS_REFUNDED:
            refunded_count = _mark_refunded(availability, request.user)
            save_status(
                settlement,
                _slot_key(slot),
                STATUS_REFUNDED,
                request.user,
                legacy_keys=_legacy_keys(slot),
            )
            messages.success(
                request,
                f"コート代の返金済みを登録しました。対象経費{refunded_count}件を精算対象外にしました。",
            )

        elif action == "court_not_required":
            status = _status_entry(
                read_status_map(settlement),
                slot,
            ).get("status")
            if status != STATUS_HELD:
                messages.error(
                    request,
                    "実施済みのレッスンだけコート代なしにできます。",
                )
                return redirect(redirect_url)
            created = _mark_court_cost_not_required(
                availability,
                request.user,
            )
            if created:
                messages.success(
                    request,
                    "コート代なしとして確認済みにしました。",
                )
            else:
                messages.error(
                    request,
                    "登録済みのコート代があります。修正画面でご確認ください。",
                )

        else:
            messages.error(request, "変更内容が正しくありません。")
            return redirect(redirect_url)

        calculate_monthly_settlement(
            selected_year,
            selected_month,
            force=True,
        )
        return redirect(redirect_url)

    status_map = read_status_map(settlement)
    rows = []
    counts = {
        STATUS_UNCONFIRMED: 0,
        STATUS_SCHEDULED: 0,
        STATUS_HELD: 0,
        STATUS_RAIN_CANCELED: 0,
        STATUS_REFUND_PENDING: 0,
        STATUS_REFUNDED: 0,
        "court_registered": 0,
        "court_unregistered": 0,
        "court_not_required": 0,
    }
    month_start, next_month = _month_range(selected_year, selected_month)
    court_expenses = list(
        CoachExpense.objects.filter(
            expense_date__gte=month_start,
            expense_date__lt=next_month,
            category=CoachExpense.CATEGORY_COURT,
        )
        .select_related("created_by")
        .order_by("-id")
    )

    for slot in slots:
        availability = slot["availability"]
        reservations = list(_reservation_queryset(slot))
        entry = _status_entry(status_map, slot)
        saved_status = entry.get("status")

        if saved_status in STATUS_LABELS:
            status = saved_status
        elif slot["end_at"] > timezone.now():
            status = STATUS_SCHEDULED
        else:
            status = STATUS_UNCONFIRMED

        counts[status] = counts.get(status, 0) + 1
        active_count = sum(
            1
            for reservation in reservations
            if reservation.status == Reservation.STATUS_ACTIVE
        )
        canceled_count = sum(
            1
            for reservation in reservations
            if reservation.status == Reservation.STATUS_CANCELED
        )
        court_expense, court_meta = _court_expense_for_availability(
            court_expenses,
            availability,
        )
        approval_status = court_meta.get("approval_status", "")
        court_not_required = bool(
            court_meta.get("court_cost_not_required")
        )
        court_registered = bool(
            court_expense is not None
            and approval_status == "approved"
        )

        if status in (STATUS_RAIN_CANCELED, STATUS_REFUND_PENDING):
            court_status = "refund_pending" if court_expense else "not_required"
            court_status_label = (
                "返金待ち"
                if court_expense
                else "登録不要"
            )
        elif status == STATUS_REFUNDED:
            court_status = "not_required"
            court_status_label = "返金済み"
        elif court_not_required:
            court_status = "not_required"
            court_status_label = "コート代なし"
        elif court_registered:
            court_status = "registered"
            court_status_label = "登録済み"
        elif status == STATUS_HELD:
            court_status = "unregistered"
            court_status_label = "未登録"
        elif status == STATUS_SCHEDULED:
            court_status = "scheduled"
            court_status_label = "開催後に確認"
        else:
            court_status = "waiting"
            court_status_label = "実施確認後"

        if court_status == "registered":
            counts["court_registered"] += 1
        elif court_status == "unregistered":
            counts["court_unregistered"] += 1
        elif court_status == "not_required":
            counts["court_not_required"] += 1

        needs_attention = bool(
            status in (STATUS_UNCONFIRMED, STATUS_REFUND_PENDING)
            or court_status == "unregistered"
        )

        rows.append(
            {
                "availability": availability,
                "start_local": _local(slot["start_at"]),
                "end_local": _local(slot["end_at"]),
                "coach_names": slot["coach_names"],
                "status": status,
                "status_label": STATUS_LABELS[status],
                "status_class": STATUS_CLASSES[status],
                "active_count": active_count,
                "canceled_count": canceled_count,
                "can_mark_held": (
                    slot["end_at"] <= timezone.now()
                    and active_count > 0
                    and status not in (
                        STATUS_RAIN_CANCELED,
                        STATUS_REFUND_PENDING,
                        STATUS_REFUNDED,
                    )
                ),
                "can_mark_rain": status
                not in (
                    STATUS_HELD,
                    STATUS_REFUNDED,
                    STATUS_REFUND_PENDING,
                ),
                "can_mark_refunded": status
                in (STATUS_RAIN_CANCELED, STATUS_REFUND_PENDING),
                "updated_by_name": entry.get("updated_by_name", ""),
                "source_kind": slot["source_kind"],
                "court_status": court_status,
                "court_status_label": court_status_label,
                "court_amount": (
                    int(court_expense.amount or 0)
                    if court_expense is not None
                    else None
                ),
                "court_payer_name": (
                    _display_name(court_expense.created_by)
                    if court_expense is not None
                    and not court_not_required
                    else ""
                ),
                "court_expense_url": (
                    f"{reverse('club:coach_expense_manage')}?"
                    f"availability_id={availability.pk}"
                    f"&date={_local(slot['start_at']).date().isoformat()}"
                ),
                "can_mark_court_not_required": (
                    status == STATUS_HELD
                    and court_status == "unregistered"
                ),
                "needs_attention": needs_attention,
            }
        )

    if pending_only:
        rows = [row for row in rows if row["needs_attention"]]

    prev_year, prev_month = _previous_month(
        selected_year,
        selected_month,
    )
    next_year, next_month_value = _next_month(
        selected_year,
        selected_month,
    )

    return render(
        request,
        "coach/lesson_execution_manage.html",
        {
            "rows": rows,
            "selected_year": selected_year,
            "selected_month": selected_month,
            "month_label": f"{selected_year}年{selected_month}月",
            "prev_url": _month_url(
                prev_year,
                prev_month,
                pending_only=pending_only,
            ),
            "next_url": _month_url(
                next_year,
                next_month_value,
                pending_only=pending_only,
            ),
            "settlement_url": (
                f"{reverse('club:coach_admin_settlement')}?"
                f"year={selected_year}&month={selected_month}"
            ),
            "is_month_closed": settlement.is_closed,
            "counts": counts,
            "pending_only": pending_only,
            "visible_row_count": len(rows),
        },
    )
