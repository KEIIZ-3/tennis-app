from datetime import date, datetime, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone

from .models import CoachAvailability, FixedLesson, LessonWaitlist, Reservation


def _display_name(user):
    if not user:
        return "-"
    try:
        return user.display_name()
    except Exception:
        return getattr(user, "username", "-") or "-"


def _is_coach_like(user):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    return getattr(user, "role", "") in ("coach", "contractor_coach")


def _level_label(value):
    User = get_user_model()
    try:
        return dict(User.LEVEL_CHOICES).get(value, value or "-")
    except Exception:
        return value or "-"


def _lesson_level_label(obj):
    if not obj:
        return "-"
    if hasattr(obj, "target_level_display_label"):
        try:
            label = obj.target_level_display_label()
            if label:
                return label
        except Exception:
            pass

    first = getattr(obj, "target_level", "") or ""
    second = getattr(obj, "target_level_2", "") or ""
    labels = []
    for level in [first, second]:
        if level and level not in labels:
            labels.append(_level_label(level))
    return "・".join([label for label in labels if label]) or "-"


def _lesson_type_label(value):
    try:
        return dict(Reservation.LESSON_TYPE_CHOICES).get(value, value or "-")
    except Exception:
        return value or "-"


def _local_dt(value):
    if not value:
        return value
    try:
        if timezone.is_aware(value):
            return timezone.localtime(value)
    except Exception:
        pass
    return value


def _build_fixed_lesson_datetimes(fixed_lesson, target_date):
    if hasattr(fixed_lesson, "_build_datetimes_for_date"):
        return fixed_lesson._build_datetimes_for_date(target_date)

    start_hour = int(getattr(fixed_lesson, "start_hour", 0) or 0)
    start_at = datetime.combine(target_date, datetime.min.time()).replace(hour=start_hour, minute=0)
    if timezone.is_naive(start_at):
        start_at = timezone.make_aware(start_at)

    duration_hours = 2 if fixed_lesson.lesson_type == FixedLesson.LESSON_GENERAL else 1
    return start_at, start_at + timedelta(hours=duration_hours)


def _primary_coach(fixed_lesson):
    if hasattr(fixed_lesson, "primary_coach"):
        try:
            return fixed_lesson.primary_coach()
        except Exception:
            pass
    return getattr(fixed_lesson, "coach", None)


def _coach_names_from_fixed_lesson(fixed_lesson):
    if not fixed_lesson:
        return ""
    if hasattr(fixed_lesson, "coach_display_names"):
        try:
            return fixed_lesson.coach_display_names()
        except Exception:
            pass

    names = []
    for attr in ["coach", "coach_2", "coach_3"]:
        coach = getattr(fixed_lesson, attr, None)
        if coach:
            names.append(_display_name(coach))
    return " / ".join(names)


def _capacity_for_slot(availability=None, fixed_lesson=None):
    target = fixed_lesson or availability
    if target and hasattr(target, "effective_capacity"):
        try:
            return max(int(target.effective_capacity()), int(getattr(target, "capacity", 0) or 0), 1)
        except Exception:
            pass
    if target:
        return max(int(getattr(target, "capacity", 1) or 1), 1)
    return 1


def _phone_label(user):
    return (getattr(user, "phone_number", "") or "").strip()


def _member_row_from_reservation(reservation):
    user = reservation.user
    return {
        "kind": "reservation",
        "reservation": reservation,
        "name": _display_name(user),
        "phone": _phone_label(user),
        "level": _level_label(getattr(user, "member_level", "")),
        "status_label": reservation.get_status_display(),
        "detail_url": reverse("club:reservation_detail", kwargs={"pk": reservation.pk}),
        "payment_status_label": reservation.payment_status_badge_label()
        if hasattr(reservation, "payment_status_badge_label")
        else "",
        "tickets_used": int(getattr(reservation, "tickets_used", 0) or 0),
    }


def _member_row_from_fixed_member(user):
    return {
        "kind": "fixed_member",
        "reservation": None,
        "name": _display_name(user),
        "phone": _phone_label(user),
        "level": _level_label(getattr(user, "member_level", "")),
        "status_label": "固定登録",
        "detail_url": "",
        "payment_status_label": "",
        "tickets_used": "-",
    }


def _waitlist_row(waitlist):
    user = waitlist.user
    return {
        "waitlist": waitlist,
        "name": _display_name(user),
        "phone": _phone_label(user),
        "level": _level_label(getattr(user, "member_level", "")),
        "status_label": waitlist.get_status_display(),
        "created_at": waitlist.created_at,
    }


def _slot_reservation_filter(*, availability, fixed_lesson, coach, court, lesson_type, start_at, end_at):
    base = Q(
        lesson_type=lesson_type,
        start_at=start_at,
        end_at=end_at,
    )

    candidates = Q()
    if availability:
        candidates |= Q(availability=availability)
    if fixed_lesson:
        candidates |= Q(fixed_lesson=fixed_lesson)

    # カレンダーの人数カウントは「coach/court/start/end/lesson_type」のslot_key基準。
    # 予約にavailability/fixed_lessonが未紐付けの場合も拾えるように、同じ条件も併用する。
    if coach and court:
        candidates |= Q(coach=coach, court=court)
    elif coach:
        candidates |= Q(coach=coach)
    elif court:
        candidates |= Q(court=court)

    return base & candidates


def _is_2026_july_slot(start_at):
    if not start_at:
        return False

    try:
        start_local = timezone.localtime(start_at) if timezone.is_aware(start_at) else start_at
        return start_local.year == 2026 and start_local.month == 7
    except Exception:
        return False


@login_required
def lesson_calendar_member_list(request):
    is_coach_view = _is_coach_like(request.user)

    availability_id = (request.GET.get("availability_id") or "").strip()
    fixed_lesson_id = (request.GET.get("fixed_lesson_id") or "").strip()
    lesson_date_text = (request.GET.get("lesson_date") or "").strip()

    availability = None
    fixed_lesson = None
    start_at = None
    end_at = None
    coach = None
    court = None
    lesson_type = ""
    title = ""
    target_level_label = "-"

    if fixed_lesson_id and lesson_date_text:
        fixed_lesson = get_object_or_404(
            FixedLesson.objects.select_related("coach", "coach_2", "coach_3", "court").prefetch_related("members"),
            pk=fixed_lesson_id,
            is_active=True,
        )
        try:
            target_date = date.fromisoformat(lesson_date_text)
        except Exception:
            raise ValidationError("レッスン日付が正しくありません。")

        start_at, end_at = _build_fixed_lesson_datetimes(fixed_lesson, target_date)
        coach = _primary_coach(fixed_lesson)
        court = fixed_lesson.court
        lesson_type = fixed_lesson.lesson_type
        title = fixed_lesson.title or fixed_lesson.get_lesson_type_display()
        target_level_label = _lesson_level_label(fixed_lesson)

        if availability_id:
            availability = (
                CoachAvailability.objects.select_related("coach", "substitute_coach", "court")
                .filter(pk=availability_id)
                .first()
            )

        if not availability:
            availability = (
                CoachAvailability.objects.select_related("coach", "substitute_coach", "court")
                .filter(
                    coach=coach,
                    lesson_type=lesson_type,
                    start_at=start_at,
                    end_at=end_at,
                )
                .filter(Q(court=court) | Q(court__isnull=True))
                .order_by("id")
                .first()
            )

        if availability:
            coach = availability.coach
            court = availability.court or court
    elif availability_id:
        availability = get_object_or_404(
            CoachAvailability.objects.select_related("coach", "substitute_coach", "court"),
            pk=availability_id,
        )
        start_at = availability.start_at
        end_at = availability.end_at
        coach = availability.coach
        court = availability.court
        lesson_type = availability.lesson_type
        title = availability.get_lesson_type_display()
        target_level_label = _lesson_level_label(availability)
    else:
        return HttpResponse("対象レッスンが見つかりません。", status=404)

    is_public_member_view = (not is_coach_view) and _is_2026_july_slot(start_at)
    if not is_coach_view and not is_public_member_view:
        return HttpResponse("Forbidden", status=403)

    reservation_filter = _slot_reservation_filter(
        availability=availability,
        fixed_lesson=fixed_lesson,
        coach=coach,
        court=court,
        lesson_type=lesson_type,
        start_at=start_at,
        end_at=end_at,
    )

    active_reservations = list(
        Reservation.objects.select_related("user", "coach", "substitute_coach", "court", "fixed_lesson", "availability")
        .filter(reservation_filter, status=Reservation.STATUS_ACTIVE)
        .order_by("user__full_name", "user__username", "id")
        .distinct()
    )

    pending_reservations = list(
        Reservation.objects.select_related("user", "coach", "substitute_coach", "court", "fixed_lesson", "availability")
        .filter(reservation_filter, status=Reservation.STATUS_PENDING)
        .order_by("user__full_name", "user__username", "id")
        .distinct()
    )

    waitlists = list(
        LessonWaitlist.objects.select_related("user", "coach", "substitute_coach", "court", "fixed_lesson", "availability")
        .filter(
            lesson_type=lesson_type,
            start_at=start_at,
            end_at=end_at,
            status=LessonWaitlist.STATUS_WAITING,
        )
        .filter(
            Q(availability=availability)
            | Q(fixed_lesson=fixed_lesson)
            | Q(coach=coach, court=court)
        )
        .order_by("created_at", "id")
        .distinct()
    )

    active_user_ids = {reservation.user_id for reservation in active_reservations}
    pending_user_ids = {reservation.user_id for reservation in pending_reservations}

    fixed_member_rows = []
    if fixed_lesson:
        try:
            for member in fixed_lesson.members.all().order_by("full_name", "username", "id"):
                if member.pk in active_user_ids or member.pk in pending_user_ids:
                    continue
                fixed_member_rows.append(_member_row_from_fixed_member(member))
        except Exception:
            fixed_member_rows = []

    active_rows = [_member_row_from_reservation(reservation) for reservation in active_reservations]
    active_rows.extend(fixed_member_rows)

    capacity = _capacity_for_slot(availability=availability, fixed_lesson=fixed_lesson)
    active_count = len(active_rows)
    waitlist_count = len(waitlists)
    pending_count = len(pending_reservations)

    if fixed_lesson:
        coach_name = _coach_names_from_fixed_lesson(fixed_lesson)
    elif availability:
        coach_name = _display_name(availability.assigned_coach())
    else:
        coach_name = _display_name(coach)

    return render(
        request,
        "coach/lesson_member_list.html",
        {
            "title": title,
            "lesson_type_label": _lesson_type_label(lesson_type),
            "target_level_label": target_level_label,
            "coach_name": coach_name,
            "court_name": str(court or "-"),
            "start_at": _local_dt(start_at),
            "end_at": _local_dt(end_at),
            "capacity": capacity,
            "active_count": active_count,
            "remaining_count": max(capacity - active_count, 0),
            "pending_count": pending_count,
            "waitlist_count": waitlist_count,
            "active_rows": active_rows,
            "pending_rows": [_member_row_from_reservation(reservation) for reservation in pending_reservations],
            "waitlist_rows": [_waitlist_row(waitlist) for waitlist in waitlists],
            "back_year": request.GET.get("year") or "",
            "back_month": request.GET.get("month") or "",
            "is_public_member_view": is_public_member_view,
            "is_coach_view": is_coach_view,
        },
    )
