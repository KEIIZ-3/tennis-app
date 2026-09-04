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
from .court_fee_service import calculate_availability_court_fee
from .expense_metadata import build_expense_note
from .lesson_participants import (
    ALL_RESERVATION_STATUSES,
    reservations_for_lesson,
)
from .models import (
    CoachAvailability,
    CoachExpense,
    Court,
    FixedLesson,
    RainRefund,
    Reservation,
)
from .settlement_balance_policy import main_coaches
from .settlement_service import calculate_monthly_settlement, get_or_create_monthly_settlement


STATUS_UNCONFIRMED = "unconfirmed"
STATUS_SCHEDULED = "scheduled"
STATUS_HELD = "held"
STATUS_RAIN_CANCELED = "rain_canceled"
STATUS_REFUND_PENDING = "refund_pending"
STATUS_REFUNDED = "refunded"
ACTION_UNHOLD = "unhold"
CANCELLATION_TYPE_RAIN = "rain"
CANCELLATION_TYPE_OTHER = "other"
CANCELLATION_TYPE_LABELS = {
    CANCELLATION_TYPE_RAIN: "雨天中止",
    CANCELLATION_TYPE_OTHER: "中止",
}

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


def _cancellation_evidence(reservations):
    canceled = [
        reservation for reservation in reservations
        if reservation.status in (
            Reservation.STATUS_CANCELED,
            Reservation.STATUS_RAIN_CANCELED,
        )
    ]
    if not canceled or any(
        reservation.status in (
            Reservation.STATUS_ACTIVE,
            Reservation.STATUS_PENDING,
        )
        for reservation in reservations
    ):
        return None
    if not any(
        reservation.status == Reservation.STATUS_RAIN_CANCELED
        or "雨天中止" in str(reservation.cancellation_reason or "")
        or "レッスン中止" in str(reservation.cancellation_reason or "")
        for reservation in canceled
    ):
        return None
    if any(
        reservation.status == Reservation.STATUS_RAIN_CANCELED
        or "雨天中止" in str(reservation.cancellation_reason or "")
        for reservation in canceled
    ):
        return CANCELLATION_TYPE_RAIN
    return CANCELLATION_TYPE_OTHER


def _effective_status(entry, reservations, *, end_at, now=None):
    """Resolve one current occurrence state, prioritizing cancellation evidence."""
    saved_status = entry.get("status")
    cancellation_type = _cancellation_evidence(reservations)
    if cancellation_type:
        if saved_status in (
            STATUS_RAIN_CANCELED,
            STATUS_REFUND_PENDING,
            STATUS_REFUNDED,
        ):
            return saved_status, entry.get("cancellation_type") or cancellation_type
        return STATUS_RAIN_CANCELED, cancellation_type
    if saved_status in STATUS_LABELS:
        return saved_status, entry.get("cancellation_type")
    return (
        (STATUS_SCHEDULED, None)
        if end_at > (now or timezone.now())
        else (STATUS_UNCONFIRMED, None)
    )


def _set_execution_status(settlement, slot, status, changed_by, *, cancellation_type=None):
    save_status(
        settlement,
        _slot_key(slot),
        status,
        changed_by,
        legacy_keys=_legacy_keys(slot),
        cancellation_type=cancellation_type,
    )


def _is_allowed(user):
    return bool(
        getattr(user, "is_staff", False)
        or getattr(user, "is_superuser", False)
        or str(getattr(user, "role", "") or "") in ("coach", "contractor_coach")
    )


def effective_status(entry, reservations, *, end_at, now=None):
    """Expose the canonical occurrence-state decision to read-only callers."""
    return _effective_status(entry or {}, reservations, end_at=end_at, now=now)


def can_manage_occurrence(user, availability, fixed_lesson=None):
    """Use the lesson-execution permission boundary for calendar entry points."""
    if not _is_allowed(user):
        return False
    return _user_can_manage_slot(
        user,
        {
            "availability": availability,
            "fixed_lesson": fixed_lesson,
        },
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
        for slot in _canonical_slots(year, month):
            if not _user_can_manage_slot(user, slot):
                continue
            availability = slot["availability"]
            entry = _status_entry(status_map, slot)
            reservations = list(_reservation_queryset(slot))
            status, cancellation_type = _effective_status(
                entry, reservations, end_at=slot["end_at"]
            )

            result[availability.pk] = {
                "execution_status": status,
                "execution_status_label": (
                    CANCELLATION_TYPE_LABELS.get(cancellation_type, "雨天中止")
                    if status in (STATUS_RAIN_CANCELED, STATUS_REFUND_PENDING, STATUS_REFUNDED)
                    else STATUS_LABELS[status]
                ),
                "cancellation_type": cancellation_type,
                "execution_needs_attention": bool(
                    status == STATUS_UNCONFIRMED
                ),
            }
    return result


def unconfirmed_execution_rows(year, month, *, now=None):
    """Return ended lesson occurrences whose execution status is still unset."""
    settlement = get_or_create_monthly_settlement(year, month)
    status_map = read_status_map(settlement)
    current_time = now or timezone.now()
    rows = []
    slots = _canonical_slots(year, month)
    reservations_by_slot = _reservations_by_slot(slots)

    for slot in slots:
        if slot["end_at"] > current_time:
            continue

        reservations = reservations_by_slot[_slot_reservation_key(slot)]
        if not any(
            reservation.status in (
                Reservation.STATUS_ACTIVE,
                Reservation.STATUS_PENDING,
            )
            for reservation in reservations
        ):
            continue
        status, _cancellation_type = effective_status(
            _status_entry(status_map, slot),
            reservations,
            end_at=slot["end_at"],
            now=current_time,
        )
        if status != STATUS_UNCONFIRMED:
            continue

        availability = slot["availability"]
        start_local = _local(slot["start_at"])
        end_local = _local(slot["end_at"])
        fixed_lesson = slot.get("fixed_lesson")
        lesson_name = (
            str(getattr(fixed_lesson, "title", "") or "").strip()
            or availability.get_lesson_type_display()
        )
        rows.append(
            {
                "availability_id": availability.pk,
                "lesson_date": start_local.date(),
                "time_label": f"{start_local:%H:%M}〜{end_local:%H:%M}",
                "coach_names": slot["coach_names"],
                "lesson_name": lesson_name,
                "registration_url": (
                    f"{reverse('club:lesson_execution_manage')}?"
                    f"year={int(year)}&month={int(month)}"
                    f"#lesson-{availability.pk}"
                ),
            }
        )

    return rows


def missing_rain_refund_rows(year, month):
    """雨天中止済みだが返金情報が未登録の開催枠を月次画面へ返す。"""
    settlement = get_or_create_monthly_settlement(year, month)
    status_map = read_status_map(settlement)
    registered_availability_ids = set(
        RainRefund.objects.filter(
            lesson_date__gte=_month_range(year, month)[0],
            lesson_date__lt=_month_range(year, month)[1],
            availability_id__isnull=False,
        ).values_list("availability_id", flat=True)
    )
    rows = []
    slots = _canonical_slots(year, month)
    reservations_by_slot = _reservations_by_slot(slots)
    for slot in slots:
        availability = slot["availability"]
        status = _status_entry(status_map, slot).get("status")
        has_legacy_rain_cancel = any(
            reservation.status == Reservation.STATUS_RAIN_CANCELED
            or "雨天中止" in str(reservation.cancellation_reason or "")
            for reservation in reservations_by_slot[_slot_reservation_key(slot)]
        )
        if (
            status not in (STATUS_RAIN_CANCELED, STATUS_REFUND_PENDING)
            and not has_legacy_rain_cancel
        ) or availability.pk in registered_availability_ids:
            continue
        start_local = _local(slot["start_at"])
        rows.append(
            {
                "availability_id": availability.pk,
                "lesson_date": start_local.date().isoformat(),
                "lesson_label": (
                    f"{start_local:%Y/%m/%d %H:%M} "
                    f"{availability.get_lesson_type_display()} / {availability.court}"
                ),
                "registration_url": (
                    f"{reverse('club:lesson_execution_manage')}?"
                    f"year={int(year)}&month={int(month)}"
                    f"&open_rain={availability.pk}"
                    f"#lesson-{availability.pk}"
                ),
            }
        )
    return rows


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

    return names or ["-"]


def _availability_coach_names(availability):
    if availability.substitute_coach:
        return [_display_name(availability.substitute_coach)]
    try:
        coaches = availability.all_coaches()
    except Exception:
        coaches = [
            getattr(availability, "coach", None),
            getattr(availability, "coach_2", None),
        ]
    names = [_display_name(coach) for coach in coaches if coach]
    return names or ["-"]


def _reservation_queryset(slot):
    availability = slot["availability"]
    fixed_lesson = slot.get("fixed_lesson")
    start_at = slot["start_at"]
    end_at = slot["end_at"]

    return (
        reservations_for_lesson(
            fixed_lesson=fixed_lesson,
            # A fixed occurrence is canonical even when historical reservations
            # still point at a predecessor availability for the same slot.
            availability=None if fixed_lesson is not None else availability,
            lesson_type=slot.get("lesson_type"),
            start_at=start_at,
            end_at=end_at,
            statuses=ALL_RESERVATION_STATUSES,
        )
        .select_related(
            "user",
            "coach",
            "substitute_coach",
            "fixed_lesson",
            "availability",
        )
        .order_by("id")
    )


def _slot_reservation_key(slot):
    relation_key = (
        ("fixed_lesson", slot["fixed_lesson"].pk)
        if slot.get("fixed_lesson") is not None
        else ("availability", slot["availability"].pk)
    )
    return relation_key + (slot["start_at"], slot["end_at"])


def _reservations_by_slot(slots):
    """Load canonical Reservation rows for all supplied occurrences at once."""
    grouped = {_slot_reservation_key(slot): [] for slot in slots}
    if not slots:
        return grouped

    fixed_lesson_ids = {
        slot["fixed_lesson"].pk
        for slot in slots
        if slot.get("fixed_lesson") is not None
    }
    availability_ids = {
        slot["availability"].pk
        for slot in slots
        if slot.get("fixed_lesson") is None
    }
    relation_filter = Q()
    if fixed_lesson_ids:
        relation_filter |= Q(fixed_lesson_id__in=fixed_lesson_ids)
    if availability_ids:
        relation_filter |= Q(availability_id__in=availability_ids)

    reservations = (
        Reservation.objects.filter(
            relation_filter,
            status__in=ALL_RESERVATION_STATUSES,
            start_at__gte=min(slot["start_at"] for slot in slots),
            start_at__lte=max(slot["start_at"] for slot in slots),
        )
        .select_related(
            "user",
            "coach",
            "substitute_coach",
            "fixed_lesson",
            "availability",
        )
        .order_by("id")
    )
    for reservation in reservations:
        fixed_key = (
            "fixed_lesson",
            reservation.fixed_lesson_id,
            reservation.start_at,
            reservation.end_at,
        )
        availability_key = (
            "availability",
            reservation.availability_id,
            reservation.start_at,
            reservation.end_at,
        )
        if fixed_key in grouped:
            grouped[fixed_key].append(reservation)
        if availability_key in grouped:
            grouped[availability_key].append(reservation)

    return grouped


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
            slot = {
                "availability": availability,
                "fixed_lesson": fixed_lesson,
                "target_date": target_date,
                "start_at": start_at,
                "end_at": end_at,
                "coach_names": _fixed_coach_names(fixed_lesson),
                "source_kind": "fixed_lesson",
            }
            slots.append(slot)

    fixed_reservations_by_slot = _reservations_by_slot(slots)
    for slot in slots:
        represented_availability_ids.update(
            reservation.availability_id
            for reservation in fixed_reservations_by_slot[
                _slot_reservation_key(slot)
            ]
            if reservation.availability_id is not None
        )

    extra_availabilities = (
        CoachAvailability.objects.filter(
            start_at__date__gte=month_start,
            start_at__date__lt=next_month,
        )
        .exclude(pk__in=represented_availability_ids)
        .select_related("coach", "coach_2", "substitute_coach", "court")
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
    from .rain_refund_service import confirm_rain_refund
    from .views import _court_expense_matches_availability, _expense_parse_note

    changed_count = 0
    expenses = CoachExpense.objects.filter(
        expense_date=_local(availability.start_at).date(),
        category=CoachExpense.CATEGORY_COURT,
    ).order_by("id")

    for expense in expenses:
        if not _court_expense_matches_availability(expense, availability):
            continue

        meta = _expense_parse_note(expense.note)
        if meta.get("approval_status") != EXPENSE_APPROVAL_REFUND_PENDING:
            continue
        if not (
            meta.get("rain_refund_debit_coach_id")
            and meta.get("rain_refund_payer_coach_id")
        ):
            continue
        if confirm_rain_refund(expense.pk, confirmed_by=changed_by) is not None:
            changed_count += 1

    return changed_count


def _rain_refund_input(request):
    coaches = main_coaches()
    coach_by_id = {str(coach.pk): coach for coach in coaches}
    account_value = (
        request.POST.get("rain_booking_account") or ""
    ).strip()
    collection_coach = coach_by_id.get(
        (request.POST.get("rain_collection_coach_id") or "").strip()
    )
    payer_coach = coach_by_id.get(
        (request.POST.get("rain_court_payer_id") or "").strip()
    )
    account_other = (
        request.POST.get("rain_booking_account_other") or ""
    ).strip()

    if account_value == "other":
        if not account_other:
            return None, "予約アカウント「その他」のアカウント情報を入力してください。"
        debit_coach = collection_coach
        account_coach = None
    else:
        account_coach = coach_by_id.get(account_value)
        if account_coach is None:
            return None, "予約アカウントを選択してください。"
        debit_coach = account_coach

    if collection_coach is None:
        return None, "回収予定コーチを選択してください。"

    if payer_coach is None:
        return None, "コート支払者を選択してください。"

    return {
        "account_kind": "other" if account_value == "other" else "coach",
        "account_coach": account_coach,
        "account_other": account_other,
        "collection_coach": collection_coach,
        "payer_coach": payer_coach,
        "debit_coach": debit_coach,
    }, ""


def _mark_court_expense_refund_pending(
    availability,
    *,
    changed_by,
    refund_input,
):
    from .views import (
        EXPENSE_APPROVAL_REFUND_PENDING,
        _availability_court_refund_lesson_label,
        _court_expense_matches_availability,
        _expense_parse_note,
    )

    from .court_transfer_service import (
        current_court_transfer_from_expenses,
    )

    existing_refund = (
        RainRefund.objects.select_for_update()
        .select_related("expense")
        .filter(availability=availability)
        .order_by("-id")
        .first()
    )
    if existing_refund is not None:
        return existing_refund.expense

    expenses = list(CoachExpense.objects.filter(
        expense_date=_local(availability.start_at).date(),
        category=CoachExpense.CATEGORY_COURT,
    ).select_for_update().order_by("id"))
    current_transfer = current_court_transfer_from_expenses(
        expenses,
        availability.pk,
    )
    if current_transfer is not None:
        expenses = [current_transfer] + [
            expense for expense in expenses if expense.pk != current_transfer.pk
        ]
    source_expense = None
    source_meta = {}
    for expense in expenses:
        if not _court_expense_matches_availability(expense, availability):
            continue
        meta = _expense_parse_note(expense.note)
        if int(expense.amount or 0) <= 0:
            continue
        if meta.get("approval_status") not in ("approved", "refund_pending"):
            continue

        source_expense = expense
        source_meta = meta
        break

    fee_quote = calculate_availability_court_fee(availability) or {}
    amount = int(fee_quote["total"] or 0)
    if amount <= 0:
        return None

    extra_meta = {
            key: value
            for key, value in source_meta.items()
            if key not in {
                "expense_type",
                "receipt_status",
                "receipt_check_status",
                "approval_status",
                "plain_note",
            }
        }
    account_coach = refund_input["account_coach"]
    collection_coach = refund_input["collection_coach"]
    payer_coach = refund_input["payer_coach"]
    debit_coach = refund_input["debit_coach"]
    extra_meta.update(
            {
                "record_kind": "cancellation_court_settlement",
                "availability_id": availability.pk,
                "cancellation_court_source_expense_id": (
                    source_expense.pk if source_expense else None
                ),
                "rain_refund_account_kind": refund_input["account_kind"],
                "rain_refund_account_coach_id": (
                    account_coach.pk if account_coach else None
                ),
                "rain_refund_account_name": (
                    _display_name(account_coach)
                    if account_coach
                    else refund_input["account_other"]
                ),
                "rain_refund_account_other": refund_input["account_other"],
                "rain_refund_collection_coach_id": (
                    collection_coach.pk if collection_coach else None
                ),
                "rain_refund_collection_coach_name": (
                    _display_name(collection_coach) if collection_coach else ""
                ),
                "rain_refund_payer_coach_id": payer_coach.pk,
                "rain_refund_payer_coach_name": _display_name(payer_coach),
                "rain_refund_debit_coach_id": debit_coach.pk,
                "rain_refund_debit_coach_name": _display_name(debit_coach),
                "rain_canceled_at": timezone.now().isoformat(),
                "rain_canceled_by_id": getattr(changed_by, "pk", None),
                "rain_canceled_by_name": _display_name(changed_by),
            }
    )
    expense = CoachExpense(
        expense_date=_local(availability.start_at).date(),
        category=CoachExpense.CATEGORY_COURT,
        amount=amount,
        created_by=payer_coach,
    )
    expense.note = build_expense_note(
        {
            "expense_type": "court_transfer",
            "receipt_status": source_meta.get("receipt_status", "none"),
            "receipt_check_status": source_meta.get(
                "receipt_check_status", "checked"
            ),
            "approval_status": EXPENSE_APPROVAL_REFUND_PENDING,
            **extra_meta,
        },
        "中止時コート精算",
    )
    expense.full_clean()
    expense.save()
    RainRefund.objects.create(
        expense=expense,
        availability=availability,
        lesson_date=_local(availability.start_at).date(),
        lesson_label=source_meta.get(
                    "court_refund_lesson_label",
                    _availability_court_refund_lesson_label(availability),
        ),
        amount=amount,
        status=RainRefund.STATUS_PENDING,
        booking_account_kind=refund_input["account_kind"],
        booking_account_coach=account_coach,
        booking_account_other=refund_input["account_other"],
        collection_coach=collection_coach,
        debit_coach=debit_coach,
        payer_coach=payer_coach,
        confirmed_at=None,
        confirmed_by=None,
    )
    return expense


def _court_expense_for_availability(expenses, availability):
    from .court_transfer_service import current_court_transfer_from_expenses
    from .expense_metadata import parse_expense_note as _parse_transfer_note
    from .views import _court_expense_matches_availability, _expense_parse_note

    current_transfer = current_court_transfer_from_expenses(
        expenses,
        availability.pk,
    )
    if current_transfer is not None:
        return current_transfer, _parse_transfer_note(current_transfer.note)

    for expense in expenses:
        if _court_expense_matches_availability(expense, availability):
            return expense, _expense_parse_note(expense.note)
    return None, {}


def _mark_court_cost_not_required(availability, changed_by):
    from .court_expense_transfer import (
        APPROVAL_APPROVED,
        RECORD_KIND,
        _existing_transfer_for_availability,
        _facility_label,
        _lesson_label,
        _slot_key as _court_slot_key,
        _using_coaches,
    )
    from .expense_metadata import build_expense_note

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
        expense.note = build_expense_note(meta, "コート代なし")
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
    open_rain_id = str(request.GET.get("open_rain") or "").strip()
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

            if _cancellation_evidence(reservations):
                messages.error(request, "中止済みのレッスンは実施登録できません。")
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

            _set_execution_status(settlement, slot, STATUS_HELD, request.user)
            messages.success(
                request,
                "レッスンを実施済みにしました。売上とコート代の精算対象になります。",
            )

        elif action == ACTION_UNHOLD:
            entry = _status_entry(read_status_map(settlement), slot)
            effective_status, cancellation_type = _effective_status(
                entry, reservations, end_at=slot["end_at"]
            )
            if effective_status != STATUS_HELD:
                messages.error(request, "実施登録済みのレッスンだけ解除できます。")
                return redirect(redirect_url)
            _set_execution_status(
                settlement, slot, STATUS_UNCONFIRMED, request.user
            )
            messages.success(
                request,
                "実施登録を解除しました。売上と精算の実施済み対象から除外しました。",
            )

        elif action == STATUS_RAIN_CANCELED:
            cancellation_type = (
                request.POST.get("cancellation_type")
                or CANCELLATION_TYPE_RAIN
            ).strip()
            if cancellation_type not in CANCELLATION_TYPE_LABELS:
                messages.error(request, "中止種別を選択してください。")
                return redirect(redirect_url)
            cancellation_label = CANCELLATION_TYPE_LABELS[cancellation_type]
            refund_input, input_error = _rain_refund_input(request)
            if input_error:
                messages.error(request, input_error)
                return redirect(redirect_url)
            canceled_count = 0
            with transaction.atomic():
                CoachAvailability.objects.select_for_update().get(
                    pk=availability.pk
                )
                pending_expense = _mark_court_expense_refund_pending(
                    availability,
                    changed_by=request.user,
                    refund_input=refund_input,
                )
                if pending_expense is None:
                    messages.error(
                        request,
                        "コート代を自動計算できませんでした。コートと開催時間を確認してください。",
                    )
                    return redirect(redirect_url)
                for reservation in reservations:
                    if reservation.status not in (
                        Reservation.STATUS_ACTIVE,
                        Reservation.STATUS_PENDING,
                    ):
                        continue
                    if cancellation_type == CANCELLATION_TYPE_RAIN:
                        reservation.mark_rain_canceled(
                            created_by=request.user,
                            reason="雨天中止による自動返却",
                        )
                    else:
                        reservation.cancel(
                            created_by=request.user,
                            reason="レッスン中止による自動返却",
                            schedule_notification=False,
                        )
                    canceled_count += 1

                _set_execution_status(
                    settlement,
                    slot,
                    STATUS_REFUND_PENDING,
                    request.user,
                    cancellation_type=cancellation_type,
                )

            messages.success(
                request,
                f"{cancellation_label}を登録しました。予約{canceled_count}件をキャンセルし、チケットを返却しました。",
            )

        elif action == STATUS_REFUNDED:
            refunded_count = _mark_refunded(availability, request.user)
            if refunded_count <= 0:
                messages.error(
                    request,
                    "返金待ちのコート代を確認できませんでした。",
                )
                return redirect(redirect_url)
            previous_entry = _status_entry(read_status_map(settlement), slot)
            save_status(
                settlement,
                _slot_key(slot),
                STATUS_REFUNDED,
                request.user,
                legacy_keys=_legacy_keys(slot),
                cancellation_type=previous_entry.get("cancellation_type"),
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
    rain_refunds_by_availability_id = {
        refund.availability_id: refund
        for refund in RainRefund.objects.filter(
            lesson_date__gte=month_start,
            lesson_date__lt=next_month,
            availability_id__isnull=False,
        ).select_related(
            "expense", "booking_account_coach", "collection_coach", "payer_coach"
        ).order_by("id")
    }
    rain_refund_availability_ids = set(rain_refunds_by_availability_id)

    for slot in slots:
        availability = slot["availability"]
        reservations = list(_reservation_queryset(slot))
        entry = _status_entry(status_map, slot)
        saved_status = entry.get("status")
        status, cancellation_type = _effective_status(
            entry, reservations, end_at=slot["end_at"]
        )
        has_cancellation_conflict = bool(
            _cancellation_evidence(reservations) and saved_status == STATUS_HELD
        )

        counts[status] = counts.get(status, 0) + 1
        active_count = sum(
            1
            for reservation in reservations
            if reservation.status == Reservation.STATUS_ACTIVE
        )
        canceled_count = sum(
            1
            for reservation in reservations
            if reservation.status in (
                Reservation.STATUS_CANCELED,
                Reservation.STATUS_RAIN_CANCELED,
            )
        )
        court_expense, court_meta = _court_expense_for_availability(
            court_expenses,
            availability,
        )
        cancellation_refund = rain_refunds_by_availability_id.get(availability.pk)
        if cancellation_refund is not None:
            court_expense = cancellation_refund.expense
            from .expense_metadata import parse_expense_note
            court_meta = parse_expense_note(court_expense.note)
        rain_refund_exists = availability.pk in rain_refund_availability_ids
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
                "status_label": (
                    CANCELLATION_TYPE_LABELS.get(cancellation_type, "雨天中止")
                    if status in (
                        STATUS_RAIN_CANCELED,
                        STATUS_REFUND_PENDING,
                        STATUS_REFUNDED,
                    )
                    else STATUS_LABELS[status]
                ),
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
                "can_unhold": status == STATUS_HELD,
                "can_mark_rain": status
                not in (
                    STATUS_REFUNDED,
                    STATUS_REFUND_PENDING,
                ) or has_cancellation_conflict,
                "can_mark_refunded": (
                    rain_refund_exists
                    and status in (
                        STATUS_RAIN_CANCELED,
                        STATUS_REFUND_PENDING,
                    )
                ),
                "updated_by_name": entry.get("updated_by_name", ""),
                "source_kind": slot["source_kind"],
                "court_status": court_status,
                "court_status_label": court_status_label,
                "court_amount": (
                    int(cancellation_refund.amount)
                    if cancellation_refund is not None
                    else (
                        int(court_expense.amount or 0)
                        if court_expense is not None
                        else int((calculate_availability_court_fee(availability) or {}).get("total") or 0)
                    )
                ),
                "court_payer_name": (
                    _display_name(court_expense.created_by)
                    if court_expense is not None
                    and not court_not_required
                    else ""
                ),
                "rain_refund_account_name": court_meta.get(
                    "rain_refund_account_name",
                    "",
                ),
                "rain_refund_collection_coach_name": court_meta.get(
                    "rain_refund_collection_coach_name",
                    "",
                ),
                "rain_refund_payer_coach_name": court_meta.get(
                    "rain_refund_payer_coach_name",
                    "",
                ),
                "cancellation_type": cancellation_type,
                "cancellation_court_fee_quote": (
                    calculate_availability_court_fee(availability) or {"total": 0}
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
                "rain_refund_missing": bool(
                    status in (STATUS_RAIN_CANCELED, STATUS_REFUND_PENDING)
                    and not rain_refund_exists
                ),
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
            "main_coach_options": main_coaches(),
            "open_rain_id": open_rain_id,
        },
    )
