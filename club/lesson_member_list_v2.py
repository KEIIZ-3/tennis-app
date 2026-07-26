from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from . import lesson_member_list as legacy
from .models import CoachAvailability, FixedLesson, LessonWaitlist, Reservation


@login_required
def lesson_calendar_member_list(request):
    """開催回の参加者を有効な予約レコードだけから表示する。

    FixedLesson.members は将来の固定予約を生成する設定であり、個別開催回の
    参加実績ではない。キャンセル済み固定メンバーを設定情報から補完しない。
    """
    is_coach_view = legacy._is_coach_like(request.user)

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
            FixedLesson.objects.select_related(
                "coach", "coach_2", "coach_3", "court"
            ),
            pk=fixed_lesson_id,
            is_active=True,
        )
        try:
            target_date = date.fromisoformat(lesson_date_text)
        except Exception as exc:
            raise ValidationError("レッスン日付が正しくありません。") from exc

        start_at, end_at = legacy._build_fixed_lesson_datetimes(
            fixed_lesson, target_date
        )
        coach = legacy._primary_coach(fixed_lesson)
        court = fixed_lesson.court
        lesson_type = fixed_lesson.lesson_type
        title = fixed_lesson.title or fixed_lesson.get_lesson_type_display()
        target_level_label = legacy._lesson_level_label(fixed_lesson)

        if availability_id:
            availability = (
                CoachAvailability.objects.select_related(
                    "coach", "substitute_coach", "court"
                )
                .filter(pk=availability_id)
                .first()
            )

        if not availability:
            availability_qs = CoachAvailability.objects.select_related(
                "coach", "substitute_coach", "court"
            ).filter(
                lesson_type=lesson_type,
                start_at=start_at,
                end_at=end_at,
            )
            if court:
                availability_qs = availability_qs.filter(
                    Q(court=court) | Q(court__isnull=True)
                )
            availability = availability_qs.order_by("id").first()

        if availability:
            court = availability.court or court

    elif availability_id:
        availability = get_object_or_404(
            CoachAvailability.objects.select_related(
                "coach", "substitute_coach", "court"
            ),
            pk=availability_id,
        )
        start_at = availability.start_at
        end_at = availability.end_at
        coach = availability.coach
        court = availability.court
        lesson_type = availability.lesson_type
        title = availability.get_lesson_type_display()
        target_level_label = legacy._lesson_level_label(availability)
    else:
        return HttpResponse("対象レッスンが見つかりません。", status=404)

    if is_coach_view and not legacy._contractor_can_access_lesson(
        request.user,
        fixed_lesson=fixed_lesson,
        availability=availability,
    ):
        return HttpResponse("Forbidden", status=403)

    if request.method == "POST":
        if not is_coach_view:
            return HttpResponse("Forbidden", status=403)

        action = (request.POST.get("action") or "").strip()
        if action not in ("close_recruitment", "reopen_recruitment"):
            return HttpResponse("Bad Request", status=400)

        if availability is None and fixed_lesson is not None:
            if not coach or not court:
                messages.error(
                    request, "募集状態を保存できるレッスン枠が見つかりません。"
                )
                return redirect(request.get_full_path())
            availability = CoachAvailability.objects.create(
                coach=coach,
                court=court,
                lesson_type=lesson_type,
                target_level=fixed_lesson.target_level,
                target_level_2=getattr(fixed_lesson, "target_level_2", "") or "",
                start_at=start_at,
                end_at=end_at,
                capacity=legacy._capacity_for_slot(fixed_lesson=fixed_lesson),
                coach_count=max(
                    int(getattr(fixed_lesson, "coach_count", 1) or 1), 1
                ),
                court_count=max(
                    int(getattr(fixed_lesson, "court_count", 1) or 1), 1
                ),
                status=CoachAvailability.STATUS_OPEN,
                note=f"固定レッスン: {title}",
            )

        availability.is_recruitment_closed = action == "close_recruitment"
        availability.save(update_fields=["is_recruitment_closed"])
        messages.success(
            request,
            "このレッスンの参加者募集を終了しました。"
            if availability.is_recruitment_closed
            else "このレッスンの参加者募集を再開しました。",
        )
        return redirect(request.get_full_path())

    is_public_member_view = (not is_coach_view) and legacy._is_2026_july_slot(
        start_at
    )
    if not is_coach_view and not is_public_member_view:
        return HttpResponse("Forbidden", status=403)

    reservation_filter = legacy._slot_reservation_filter(
        availability=availability,
        fixed_lesson=fixed_lesson,
        coach=coach,
        court=court,
        lesson_type=lesson_type,
        start_at=start_at,
        end_at=end_at,
    )

    reservation_base = Reservation.objects.select_related(
        "user",
        "coach",
        "substitute_coach",
        "court",
        "fixed_lesson",
        "availability",
    ).filter(reservation_filter)

    active_reservations = list(
        reservation_base.filter(status=Reservation.STATUS_ACTIVE)
        .order_by("user__full_name", "user__username", "id")
        .distinct()
    )
    pending_reservations = list(
        reservation_base.filter(status=Reservation.STATUS_PENDING)
        .order_by("user__full_name", "user__username", "id")
        .distinct()
    )
    participant_snapshot_map = legacy._reservation_participant_snapshot_map(
        active_reservations + pending_reservations
    )

    waitlist_filter = Q(
        lesson_type=lesson_type,
        start_at=start_at,
        end_at=end_at,
        status=LessonWaitlist.STATUS_WAITING,
    )
    waitlist_relation_filter = Q()
    if availability:
        waitlist_relation_filter |= Q(availability=availability)
    if fixed_lesson:
        waitlist_relation_filter |= Q(fixed_lesson=fixed_lesson)
    if coach and court:
        waitlist_relation_filter |= Q(coach=coach, court=court)

    waitlists = list(
        LessonWaitlist.objects.select_related(
            "user",
            "coach",
            "substitute_coach",
            "court",
            "fixed_lesson",
            "availability",
        )
        .filter(waitlist_filter & waitlist_relation_filter)
        .order_by("created_at", "id")
        .distinct()
    )

    # 参加者の唯一の正本は、この開催回の有効な予約。
    active_rows = [
        legacy._member_row_from_reservation(
            reservation, participant_snapshot_map.get(reservation.pk)
        )
        for reservation in active_reservations
    ]
    pending_rows = [
        legacy._member_row_from_reservation(
            reservation, participant_snapshot_map.get(reservation.pk)
        )
        for reservation in pending_reservations
    ]

    capacity = legacy._capacity_for_slot(
        availability=availability, fixed_lesson=fixed_lesson
    )
    active_count = len(active_rows)

    if fixed_lesson:
        coach_name = legacy._coach_names_from_fixed_lesson(fixed_lesson)
    elif availability:
        coach_name = legacy._display_name(availability.assigned_coach())
    else:
        coach_name = legacy._display_name(coach)

    reservation_url = legacy._build_reservation_url(
        request,
        availability_id=availability_id,
        fixed_lesson_id=fixed_lesson_id,
        lesson_date_text=lesson_date_text,
    )

    execution_status = None
    court_summary = None
    if availability and is_coach_view:
        from . import lesson_execution
        from .court_expense_transfer import court_transfer_summary_for_availability

        status_map = lesson_execution.status_by_availability(
            request.user, {(start_at.year, start_at.month)}
        )
        execution_status = status_map.get(availability.pk)
        court_summary = court_transfer_summary_for_availability(availability)
        if (
            execution_status
            and execution_status.get("execution_status")
            in (
                lesson_execution.STATUS_RAIN_CANCELED,
                lesson_execution.STATUS_REFUND_PENDING,
                lesson_execution.STATUS_REFUNDED,
            )
            and court_summary["status"] == "unregistered"
        ):
            court_summary = {
                "status": "not_required",
                "status_label": "登録不要",
                "amount": None,
                "payer_name": "",
            }

    return render(
        request,
        "coach/lesson_member_list.html",
        {
            "title": title,
            "lesson_type_label": legacy._lesson_type_label(lesson_type),
            "target_level_label": target_level_label,
            "coach_name": coach_name,
            "court_name": str(court or "-"),
            "start_at": legacy._local_dt(start_at),
            "end_at": legacy._local_dt(end_at),
            "capacity": capacity,
            "active_count": active_count,
            "remaining_count": max(capacity - active_count, 0),
            "pending_count": len(pending_reservations),
            "waitlist_count": len(waitlists),
            "active_rows": active_rows,
            "pending_rows": pending_rows,
            "waitlist_rows": [legacy._waitlist_row(item) for item in waitlists],
            "back_year": request.GET.get("year") or "",
            "back_month": request.GET.get("month") or "",
            "is_public_member_view": is_public_member_view,
            "is_coach_view": is_coach_view,
            "is_recruitment_closed": bool(
                availability and availability.is_recruitment_closed
            ),
            "reservation_url": reservation_url,
            "execution_status": execution_status,
            "court_summary": court_summary,
        },
    )
