from django.db import connection
from django.db.models import Q
from django.urls import reverse


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


def _participant_snapshot(reservation):
    reservation_id = getattr(reservation, "pk", None)
    if not reservation_id:
        return None

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
        return None

    if not row:
        return None

    return {
        "name": row[0] or "",
        "level": row[1] or "",
    }


def _reservation_person_row(reservation):
    snapshot = _participant_snapshot(reservation) or {}
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
    }


def _related_active_reservations(row):
    from .models import Reservation

    start_at = row.get("start_at")
    end_at = row.get("end_at")
    fixed_lesson = row.get("fixed_lesson")
    availability = row.get("availability")

    if not start_at or not end_at:
        return []

    lesson_type = ""
    if fixed_lesson:
        lesson_type = getattr(fixed_lesson, "lesson_type", "") or ""
    if not lesson_type and availability:
        lesson_type = getattr(availability, "lesson_type", "") or ""
    if not lesson_type:
        reservations = row.get("reservations") or []
        if reservations:
            lesson_type = getattr(reservations[0], "lesson_type", "") or ""

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

    existing_reservations = row.get("reservations") or []
    if existing_reservations:
        first = existing_reservations[0]
        relation_filter |= Q(
            coach_id=getattr(first, "coach_id", None),
            court_id=getattr(first, "court_id", None),
        )
    elif availability:
        relation_filter |= Q(
            coach_id=getattr(availability, "coach_id", None),
            court_id=getattr(availability, "court_id", None),
        )
    elif fixed_lesson:
        primary_coach = (
            fixed_lesson.primary_coach()
            if hasattr(fixed_lesson, "primary_coach")
            else getattr(fixed_lesson, "coach", None)
        )
        relation_filter |= Q(
            coach_id=getattr(primary_coach, "pk", None),
            court_id=getattr(fixed_lesson, "court_id", None),
        )

    queryset = Reservation.objects.select_related(
        "user",
        "coach",
        "substitute_coach",
        "court",
        "fixed_lesson",
        "availability",
    ).filter(base_filter)

    if relation_filter:
        queryset = queryset.filter(relation_filter)

    return list(
        queryset.order_by("user__full_name", "user__username", "id").distinct()
    )


def _fix_lesson_row(row):
    reservations = _related_active_reservations(row)
    if not reservations:
        reservations = list(row.get("reservations") or [])

    unique_reservations = []
    seen_ids = set()
    for reservation in reservations:
        reservation_id = getattr(reservation, "pk", None)
        if not reservation_id or reservation_id in seen_ids:
            continue
        seen_ids.add(reservation_id)
        unique_reservations.append(reservation)

    row["reservations"] = unique_reservations
    row["participant_rows"] = [
        _reservation_person_row(reservation)
        for reservation in unique_reservations
    ]

    reserved_user_ids = {
        getattr(reservation, "user_id", None)
        for reservation in unique_reservations
    }

    registered_member_rows = []
    for member_row in row.get("registered_member_rows") or []:
        member = member_row.get("user")
        member_id = getattr(member, "pk", None)
        if member_id and member_id in reserved_user_ids:
            continue
        registered_member_rows.append(member_row)

    row["registered_member_rows"] = registered_member_rows

    participant_count = len(row["participant_rows"]) + len(registered_member_rows)
    capacity = int(row.get("capacity") or 0)

    row["participant_count"] = participant_count
    row["remaining_count"] = max(capacity - participant_count, 0)
    row["is_full"] = participant_count >= capacity if capacity > 0 else False

    payment_rows = [
        person
        for person in row["participant_rows"]
        if person.get("payment_required")
    ]
    row["payment_target_count"] = len(payment_rows)
    row["payment_unpaid_count"] = sum(
        1
        for person in payment_rows
        if person.get("payment_status") == "unpaid"
    )


def apply_today_lessons_count_patch():
    from . import views

    if getattr(views, "_today_lessons_count_patch_applied", False):
        return

    original_render = views.render

    def patched_render(request, template_name, context=None, *args, **kwargs):
        if template_name == "coach/today_lessons.html" and isinstance(context, dict):
            lesson_rows = context.get("lesson_rows") or []

            for row in lesson_rows:
                _fix_lesson_row(row)

            summary = context.get("summary")
            if isinstance(summary, dict):
                summary["participant_count"] = sum(
                    int(row.get("participant_count") or 0)
                    for row in lesson_rows
                )

                today_rows = context.get("today_rows") or []
                summary["today_participant_count"] = sum(
                    int(row.get("participant_count") or 0)
                    for row in today_rows
                )

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

                summary["payment_target_count"] = len(payment_reservations)
                summary["payment_paid_total"] = sum(
                    int(reservation.payment_amount or 0)
                    for reservation in payment_reservations
                    if reservation.payment_status == reservation.PAYMENT_STATUS_PAID
                )
                summary["payment_unpaid_total"] = sum(
                    int(reservation.payment_amount or 0)
                    for reservation in payment_reservations
                    if reservation.payment_status == reservation.PAYMENT_STATUS_UNPAID
                )
                summary["payment_waived_total"] = sum(
                    int(reservation.payment_amount or 0)
                    for reservation in payment_reservations
                    if reservation.payment_status == reservation.PAYMENT_STATUS_WAIVED
                )

        return original_render(request, template_name, context, *args, **kwargs)

    views.render = patched_render
    views._today_lessons_count_patch_applied = True
