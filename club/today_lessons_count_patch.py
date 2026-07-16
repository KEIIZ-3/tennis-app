import copy
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlencode

from django.db import connection
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import redirect, render
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
        for fixed in queryset:
            if str(getattr(fixed, "court", "") or "").strip() == court_name:
                return fixed

    return queryset.first()


def _related_active_reservations(row, fixed_lesson):
    from .lesson_member_list import _slot_reservation_filter
    from .models import Reservation

    start_at = row.get("start_at")
    end_at = row.get("end_at")
    availability = row.get("availability")

    if not start_at or not end_at:
        return list(row.get("reservations") or [])

    current_reservations = list(row.get("reservations") or [])

    lesson_type = ""
    coach = None
    court = None

    if fixed_lesson:
        lesson_type = getattr(fixed_lesson, "lesson_type", "") or ""
        coach = (
            fixed_lesson.primary_coach()
            if hasattr(fixed_lesson, "primary_coach")
            else getattr(fixed_lesson, "coach", None)
        )
        court = getattr(fixed_lesson, "court", None)

    if availability:
        lesson_type = getattr(availability, "lesson_type", "") or lesson_type
        coach = getattr(availability, "coach", None) or coach
        court = getattr(availability, "court", None) or court

    if current_reservations:
        first_reservation = current_reservations[0]
        lesson_type = getattr(first_reservation, "lesson_type", "") or lesson_type
        coach = getattr(first_reservation, "coach", None) or coach
        court = getattr(first_reservation, "court", None) or court

    if not lesson_type:
        return current_reservations

    reservation_filter = _slot_reservation_filter(
        availability=availability,
        fixed_lesson=fixed_lesson,
        coach=coach,
        court=court,
        lesson_type=lesson_type,
        start_at=start_at,
        end_at=end_at,
    )

    return list(
        Reservation.objects.select_related(
            "user",
            "coach",
            "substitute_coach",
            "court",
            "fixed_lesson",
            "availability",
        )
        .filter(
            reservation_filter,
            status=Reservation.STATUS_ACTIVE,
        )
        .order_by("user__full_name", "user__username", "id")
        .distinct()
    )


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
            fixed_members = fixed_lesson.members.all().order_by(
                "full_name",
                "username",
                "id",
            )
        except Exception:
            fixed_members = []

        for member in fixed_members:
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


def _row_identity(row):
    fixed_lesson = row.get("fixed_lesson")
    availability = row.get("availability")

    if fixed_lesson:
        return (
            "fixed",
            getattr(fixed_lesson, "pk", None),
            row.get("start_at"),
            row.get("end_at"),
        )

    if availability:
        return (
            "availability",
            getattr(availability, "pk", None),
            row.get("start_at"),
            row.get("end_at"),
        )

    return (
        "slot",
        row.get("start_at"),
        row.get("end_at"),
        row.get("title"),
        row.get("court_name"),
    )


def _merge_duplicate_rows(rows):
    merged = {}

    for source_row in rows:
        row = _fix_lesson_row(source_row)
        identity = _row_identity(row)

        if identity not in merged:
            merged[identity] = row
            continue

        target = merged[identity]

        reservation_map = {
            getattr(reservation, "pk", None): reservation
            for reservation in target.get("reservations") or []
            if getattr(reservation, "pk", None)
        }

        for reservation in row.get("reservations") or []:
            reservation_id = getattr(reservation, "pk", None)
            if reservation_id:
                reservation_map[reservation_id] = reservation

        target["reservations"] = list(reservation_map.values())
        _fix_lesson_row(target)

        target["waitlist_count"] = max(
            int(target.get("waitlist_count") or 0),
            int(row.get("waitlist_count") or 0),
        )
        target["pending_count"] = max(
            int(target.get("pending_count") or 0),
            int(row.get("pending_count") or 0),
        )

    return list(merged.values())


def _recalculate_context(context):
    lesson_rows = _merge_duplicate_rows(context.get("lesson_rows") or [])

    lesson_rows.sort(
        key=lambda row: (
            row.get("start_at"),
            row.get("title") or "",
            str(_row_identity(row)),
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
            grouped_days.append(
                {
                    "date": day_cursor,
                    "date_label": f"{day_cursor:%Y/%m/%d}",
                    "weekday_label": ["月", "火", "水", "木", "金", "土", "日"][day_cursor.weekday()],
                    "is_today": day_cursor == today,
                    "rows": [
                        row
                        for row in lesson_rows
                        if row.get("date") == day_cursor
                    ],
                }
            )
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


def _capture_original_context(request):
    from . import views

    captured = {}
    original_render = views.render

    def capture_render(
        captured_request,
        template_name,
        context=None,
        *args,
        **kwargs,
    ):
        if template_name == "coach/today_lessons.html":
            captured["context"] = context or {}
            return HttpResponse("")
        return original_render(
            captured_request,
            template_name,
            context,
            *args,
            **kwargs,
        )

    views.render = capture_render
    try:
        response = views.coach_today_lessons(request)
    finally:
        views.render = original_render

    return captured.get("context"), response


def _clone_request_for_coach(request, coach_id):
    cloned_request = copy.copy(request)
    cloned_get = request.GET.copy()
    cloned_get["coach_id"] = str(coach_id)
    cloned_request.GET = cloned_get
    return cloned_request


def _admin_all_context(request):
    from . import views

    User = views.get_user_model()
    coaches = list(
        User.objects.filter(
            role__in=("coach", "contractor_coach")
        ).order_by("full_name", "username", "id")
    )

    contexts = []

    for coach in coaches:
        cloned_request = _clone_request_for_coach(request, coach.pk)
        context, _response = _capture_original_context(cloned_request)
        if isinstance(context, dict):
            contexts.append(context)

    if not contexts:
        return None

    base = dict(contexts[0])
    all_rows = []

    for context in contexts:
        all_rows.extend(context.get("lesson_rows") or [])

    base["lesson_rows"] = all_rows
    base["selected_coach"] = None
    base["selected_coach_id"] = "all"
    base["is_staff_mode"] = True
    base["coach_options"] = [
        SimpleNamespace(
            pk="all",
            display_name=lambda: "全コーチ",
        ),
        *list(base.get("coach_options") or []),
    ]

    return _recalculate_context(base)


def coach_today_lessons_view(request):
    from . import views

    admin_user = _is_admin_user(request.user)
    requested_coach_id = (
        request.GET.get("coach_id")
        or request.POST.get("coach_id")
        or ""
    ).strip()

    requested_days = (
        request.GET.get("days")
        or request.POST.get("days")
        or ""
    ).strip()

    if admin_user and not requested_days:
        requested_days = "28"

    if request.method == "POST":
        if admin_user:
            mutable_post = request.POST.copy()
            mutable_post["days"] = requested_days or "28"
            if not requested_coach_id:
                mutable_post["coach_id"] = "all"
            request.POST = mutable_post

        response = views.coach_today_lessons(request)

        if admin_user:
            return redirect(
                f"{reverse('club:coach_today_lessons')}?"
                f"{urlencode({
                    'days': requested_days or '28',
                    'coach_id': requested_coach_id or 'all',
                })}"
            )

        return response

    if admin_user:
        mutable_get = request.GET.copy()
        mutable_get["days"] = requested_days or "28"
        mutable_get["coach_id"] = requested_coach_id or "all"
        request.GET = mutable_get
        requested_coach_id = request.GET["coach_id"]

    if admin_user and requested_coach_id == "all":
        context = _admin_all_context(request)
        if context is not None:
            return render(
                request,
                "coach/today_lessons.html",
                context,
            )

    context, response = _capture_original_context(request)

    if not isinstance(context, dict):
        return response

    context = _recalculate_context(context)

    if admin_user:
        options = list(context.get("coach_options") or [])
        context["coach_options"] = [
            SimpleNamespace(
                pk="all",
                display_name=lambda: "全コーチ",
            ),
            *options,
        ]

    return render(
        request,
        "coach/today_lessons.html",
        context,
    )

def apply_today_lessons_count_patch():
    return None
