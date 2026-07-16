import copy
from types import SimpleNamespace
from urllib.parse import urlencode

from django.db import connection
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone


def _display_name(user):
    if not user:
        return "-"
    try:
        return user.display_name()
    except Exception:
        return getattr(user, "full_name", "") or getattr(user, "username", "") or str(user)


def _phone(user):
    return (
        getattr(user, "phone_number", "")
        or getattr(user, "phone", "")
        or getattr(user, "tel", "")
        or ""
    ).strip()


def _level_label(user):
    if not user:
        return "-"
    try:
        return user.get_member_level_display()
    except Exception:
        return getattr(user, "member_level", "") or "-"


def _is_admin_user(user):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    return getattr(user, "role", "") in ("admin", "staff", "manager")


def _participant_snapshot(reservation):
    reservation_id = getattr(reservation, "pk", None)
    if not reservation_id:
        return {}

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    participant_name,
                    participant_level_label
                FROM club_reservationparticipant
                WHERE reservation_id = %s
                LIMIT 1
                """,
                [reservation_id],
            )
            row = cursor.fetchone()
    except Exception:
        return {}

    if not row:
        return {}

    return {
        "name": row[0] or "",
        "level": row[1] or "",
    }


def _reservation_person_row(reservation):
    snapshot = _participant_snapshot(reservation)
    payment_status_options = [
        (reservation.PAYMENT_STATUS_UNPAID, "未回収"),
        (reservation.PAYMENT_STATUS_PAID, "回収済み"),
        (reservation.PAYMENT_STATUS_WAIVED, "免除"),
    ]

    return {
        "reservation": reservation,
        "name": snapshot.get("name") or _display_name(reservation.user),
        "phone": _phone(reservation.user),
        "level": snapshot.get("level") or _level_label(reservation.user),
        "status_label": reservation.get_status_display(),
        "detail_url": reverse("club:reservation_detail", kwargs={"pk": reservation.pk}),
        "payment_required": reservation.is_payment_tracking_required(),
        "payment_status": reservation.payment_status,
        "payment_status_label": reservation.payment_status_badge_label(),
        "payment_amount": int(reservation.payment_amount or 0),
        "payment_received_at": reservation.payment_received_at,
        "payment_status_options": payment_status_options,
        "is_fixed_member": False,
    }


def _fixed_member_person_row(member):
    return {
        "reservation": None,
        "name": _display_name(member),
        "phone": _phone(member),
        "level": _level_label(member),
        "status_label": "固定参加",
        "detail_url": "",
        "payment_required": False,
        "payment_status": "",
        "payment_status_label": "",
        "payment_amount": 0,
        "payment_received_at": None,
        "payment_status_options": [],
        "is_fixed_member": True,
    }


def _find_fixed_lesson_for_row(row):
    from .models import FixedLesson

    fixed_lesson = row.get("fixed_lesson")
    if fixed_lesson:
        return fixed_lesson

    start_at = row.get("start_at")
    if not start_at:
        return None

    start_local = timezone.localtime(start_at) if timezone.is_aware(start_at) else start_at
    lesson_type = ""

    availability = row.get("availability")
    if availability:
        lesson_type = getattr(availability, "lesson_type", "") or ""

    reservations = row.get("reservations") or []
    if not lesson_type and reservations:
        lesson_type = getattr(reservations[0], "lesson_type", "") or ""

    queryset = (
        FixedLesson.objects.filter(
            is_active=True,
            weekday=start_local.weekday(),
            start_hour=start_local.hour,
        )
        .select_related("coach", "coach_2", "coach_3", "court")
        .prefetch_related("members")
        .order_by("id")
    )

    if lesson_type:
        queryset = queryset.filter(lesson_type=lesson_type)

    court_name = str(row.get("court_name") or "").strip()
    if court_name and court_name != "-":
        matched = [
            fixed
            for fixed in queryset
            if str(getattr(fixed, "court", "") or "").strip() == court_name
        ]
        if matched:
            return matched[0]

    return queryset.first()


def _related_active_reservations(row, fixed_lesson):
    from .models import Reservation

    start_at = row.get("start_at")
    end_at = row.get("end_at")
    availability = row.get("availability")

    if not start_at or not end_at:
        return list(row.get("reservations") or [])

    lesson_type = ""
    if fixed_lesson:
        lesson_type = getattr(fixed_lesson, "lesson_type", "") or ""
    if not lesson_type and availability:
        lesson_type = getattr(availability, "lesson_type", "") or ""

    existing = row.get("reservations") or []
    if not lesson_type and existing:
        lesson_type = getattr(existing[0], "lesson_type", "") or ""

    base_filter = Q(
        start_at=start_at,
        end_at=end_at,
        status=Reservation.STATUS_ACTIVE,
    )
    if lesson_type:
        base_filter &= Q(lesson_type=lesson_type)

    relation_filter = Q()

    if fixed_lesson:
        relation_filter |= Q(fixed_lesson=fixed_lesson)

    if availability:
        relation_filter |= Q(availability=availability)

    if existing:
        for reservation in existing:
            relation_filter |= Q(
                coach_id=getattr(reservation, "coach_id", None),
                court_id=getattr(reservation, "court_id", None),
            )

    if fixed_lesson:
        primary_coach = (
            fixed_lesson.primary_coach()
            if hasattr(fixed_lesson, "primary_coach")
            else getattr(fixed_lesson, "coach", None)
        )
        relation_filter |= Q(
            coach_id=getattr(primary_coach, "pk", None),
            court_id=getattr(fixed_lesson, "court_id", None),
        )

    queryset = (
        Reservation.objects.select_related(
            "user",
            "coach",
            "substitute_coach",
            "court",
            "fixed_lesson",
            "availability",
        )
        .filter(base_filter)
        .order_by("user__full_name", "user__username", "id")
    )

    if relation_filter:
        queryset = queryset.filter(relation_filter)

    return list(queryset.distinct())


def _fix_lesson_row(row):
    fixed_lesson = _find_fixed_lesson_for_row(row)
    if fixed_lesson:
        row["fixed_lesson"] = fixed_lesson

    reservations = _related_active_reservations(row, fixed_lesson)

    unique_reservations = []
    seen_reservation_ids = set()

    for reservation in reservations:
        reservation_id = getattr(reservation, "pk", None)
        if not reservation_id or reservation_id in seen_reservation_ids:
            continue
        seen_reservation_ids.add(reservation_id)
        unique_reservations.append(reservation)

    row["reservations"] = unique_reservations

    reservation_rows = [
        _reservation_person_row(reservation)
        for reservation in unique_reservations
    ]

    reserved_user_ids = {
        getattr(reservation, "user_id", None)
        for reservation in unique_reservations
    }

    fixed_rows = []
    if fixed_lesson:
        try:
            members = fixed_lesson.members.all().order_by("full_name", "username", "id")
        except Exception:
            members = []

        for member in members:
            if member.pk in reserved_user_ids:
                continue
            fixed_rows.append(_fixed_member_person_row(member))

    row["participant_rows"] = reservation_rows + fixed_rows
    row["registered_member_rows"] = []

    participant_count = len(row["participant_rows"])
    capacity = int(row.get("capacity") or 0)

    row["participant_count"] = participant_count
    row["remaining_count"] = max(capacity - participant_count, 0)
    row["is_full"] = participant_count >= capacity if capacity > 0 else False

    payment_rows = [
        person
        for person in reservation_rows
        if person.get("payment_required")
    ]

    row["payment_target_count"] = len(payment_rows)
    row["payment_unpaid_count"] = sum(
        1
        for person in payment_rows
        if person.get("payment_status") == "unpaid"
    )

    return row


def _recalculate_context(context):
    lesson_rows = context.get("lesson_rows") or []

    for row in lesson_rows:
        _fix_lesson_row(row)

    lesson_rows.sort(
        key=lambda row: (
            row.get("start_at"),
            row.get("title") or "",
            row.get("key") or "",
        )
    )

    today = timezone.localdate()
    today_rows = [row for row in lesson_rows if row.get("date") == today]
    upcoming_rows = [row for row in lesson_rows if row.get("date") != today]
    attention_rows = [
        row
        for row in lesson_rows
        if row.get("needs_attention") and not row.get("is_past")
    ]

    context["lesson_rows"] = lesson_rows
    context["today_rows"] = today_rows
    context["upcoming_rows"] = upcoming_rows
    context["attention_rows"] = attention_rows[:10]

    range_start = context.get("range_start")
    range_end = context.get("range_end")
    grouped_days = []

    if range_start and range_end:
        day_cursor = range_start
        while day_cursor <= range_end:
            day_rows = [
                row
                for row in lesson_rows
                if row.get("date") == day_cursor
            ]
            grouped_days.append(
                {
                    "date": day_cursor,
                    "date_label": f"{day_cursor:%Y/%m/%d}",
                    "weekday_label": ["月", "火", "水", "木", "金", "土", "日"][day_cursor.weekday()],
                    "is_today": day_cursor == today,
                    "rows": day_rows,
                }
            )
            from datetime import timedelta
            day_cursor += timedelta(days=1)

    context["grouped_days"] = grouped_days

    all_reservations = []
    seen_ids = set()

    for row in lesson_rows:
        for reservation in row.get("reservations") or []:
            reservation_id = getattr(reservation, "pk", None)
            if not reservation_id or reservation_id in seen_ids:
                continue
            seen_ids.add(reservation_id)
            all_reservations.append(reservation)

    payment_reservations = [
        reservation
        for reservation in all_reservations
        if reservation.is_payment_tracking_required()
    ]

    summary = context.get("summary") or {}
    summary.update(
        {
            "lesson_count": len(lesson_rows),
            "today_lesson_count": len(today_rows),
            "participant_count": sum(
                int(row.get("participant_count") or 0)
                for row in lesson_rows
            ),
            "today_participant_count": sum(
                int(row.get("participant_count") or 0)
                for row in today_rows
            ),
            "waitlist_count": sum(
                int(row.get("waitlist_count") or 0)
                for row in lesson_rows
            ),
            "pending_count": sum(
                int(row.get("pending_count") or 0)
                for row in lesson_rows
            ),
            "attention_count": len(attention_rows),
            "payment_target_count": len(payment_reservations),
            "payment_paid_total": sum(
                int(reservation.payment_amount or 0)
                for reservation in payment_reservations
                if reservation.payment_status == reservation.PAYMENT_STATUS_PAID
            ),
            "payment_unpaid_total": sum(
                int(reservation.payment_amount or 0)
                for reservation in payment_reservations
                if reservation.payment_status == reservation.PAYMENT_STATUS_UNPAID
            ),
            "payment_waived_total": sum(
                int(reservation.payment_amount or 0)
                for reservation in payment_reservations
                if reservation.payment_status == reservation.PAYMENT_STATUS_WAIVED
            ),
        }
    )
    context["summary"] = summary

    return context


def _merge_admin_contexts(contexts):
    if not contexts:
        return None

    base = dict(contexts[0])
    merged_rows = []
    seen_keys = set()

    for context in contexts:
        for row in context.get("lesson_rows") or []:
            key = row.get("key")
            if not key:
                key = (
                    str(row.get("start_at")),
                    str(row.get("end_at")),
                    str(row.get("title")),
                    str(row.get("court_name")),
                )

            if key in seen_keys:
                continue

            seen_keys.add(key)
            merged_rows.append(row)

    base["lesson_rows"] = merged_rows
    base["selected_coach"] = None
    base["selected_coach_id"] = "all"
    base["is_staff_mode"] = True

    original_options = list(base.get("coach_options") or [])
    all_option = SimpleNamespace(
        pk="all",
        display_name=lambda: "全コーチ",
    )
    base["coach_options"] = [all_option] + original_options

    return _recalculate_context(base)


def apply_today_lessons_count_patch():
    from django.shortcuts import render as django_render
    from . import views

    if getattr(views, "_today_lessons_count_patch_applied", False):
        return

    original_view = views.coach_today_lessons
    original_render = views.render

    def patched_render(request, template_name, context=None, *args, **kwargs):
        if (
            template_name == "coach/today_lessons.html"
            and isinstance(context, dict)
        ):
            context = _recalculate_context(context)

            if _is_admin_user(request.user):
                options = list(context.get("coach_options") or [])
                if not any(str(getattr(option, "pk", "")) == "all" for option in options):
                    options.insert(
                        0,
                        SimpleNamespace(
                            pk="all",
                            display_name=lambda: "全コーチ",
                        ),
                    )
                context["coach_options"] = options

            capture_box = getattr(request, "_today_lessons_capture_box", None)
            if isinstance(capture_box, dict):
                capture_box["context"] = context
                return HttpResponse("")

        return django_render(request, template_name, context, *args, **kwargs)

    views.render = patched_render

    def wrapped_today_lessons(request, *args, **kwargs):
        admin_user = _is_admin_user(request.user)
        requested_coach_id = (
            request.GET.get("coach_id")
            or request.POST.get("coach_id")
            or ""
        ).strip()

        if request.method == "POST":
            response = original_view(request, *args, **kwargs)

            if admin_user and requested_coach_id in ("", "all"):
                try:
                    display_days = int(
                        request.GET.get("days")
                        or request.POST.get("days")
                        or 7
                    )
                except Exception:
                    display_days = 7

                url = reverse("club:coach_today_lessons")
                return redirect(
                    f"{url}?{urlencode({'days': display_days, 'coach_id': 'all'})}"
                )

            return response

        if not admin_user or requested_coach_id not in ("", "all"):
            return original_view(request, *args, **kwargs)

        User = views.get_user_model()
        coaches = list(
            User.objects.filter(
                role__in=("coach", "contractor_coach")
            ).order_by("full_name", "username", "id")
        )

        captured_contexts = []

        for coach in coaches:
            cloned_request = copy.copy(request)
            cloned_get = request.GET.copy()
            cloned_get["coach_id"] = str(coach.pk)
            cloned_request.GET = cloned_get

            capture_box = {}
            cloned_request._today_lessons_capture_box = capture_box

            original_view(cloned_request, *args, **kwargs)

            context = capture_box.get("context")
            if isinstance(context, dict):
                captured_contexts.append(context)

        merged_context = _merge_admin_contexts(captured_contexts)

        if merged_context is None:
            return original_view(request, *args, **kwargs)

        return django_render(
            request,
            "coach/today_lessons.html",
            merged_context,
        )

    views.coach_today_lessons = wrapped_today_lessons
    views._today_lessons_count_patch_applied = True
