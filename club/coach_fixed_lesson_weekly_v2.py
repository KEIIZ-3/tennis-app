from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from . import views as legacy
from .models import CoachAvailability, FixedLesson, LessonWaitlist, Reservation


@login_required
@require_GET
def coach_fixed_lesson_weekly(request):
    """固定レッスン週間一覧を、正式な開催日と有効予約だけで集計する。"""
    if not (legacy._is_coach_user(request.user) or legacy._is_staff_like(request.user)):
        return HttpResponse("Forbidden", status=403)

    User = get_user_model()
    today = timezone.localdate()
    coach_queryset = User.objects.filter(
        role__in=("coach", "contractor_coach")
    ).order_by("full_name", "username", "id")

    if legacy._is_coach_user(request.user):
        selected_coach = request.user
        selected_coach_id = str(request.user.pk)
        is_staff_mode = False
    else:
        selected_coach_id = (request.GET.get("coach_id") or "").strip()
        selected_coach = (
            coach_queryset.filter(pk=selected_coach_id).first()
            if selected_coach_id
            else coach_queryset.first()
        )
        selected_coach_id = str(selected_coach.pk) if selected_coach else ""
        is_staff_mode = True

    display_weeks = 12
    display_until = today + timedelta(days=display_weeks * 7)

    rows = []
    fixed_queryset = (
        FixedLesson.objects.filter(is_active=True)
        .select_related("coach", "coach_2", "coach_3", "court")
        .order_by("weekday", "start_hour", "id")
    )
    weekday_labels = dict(FixedLesson.WEEKDAY_CHOICES)

    for fixed in fixed_queryset:
        occurrence_dates = [
            target_date
            for target_date in fixed.scheduled_occurrence_dates()
            if today <= target_date <= display_until
        ]

        for target_date in occurrence_dates:
            start_at, end_at = fixed._build_datetimes_for_date(target_date)
            availability = (
                CoachAvailability.objects.filter(
                    lesson_type=fixed.lesson_type,
                    start_at=start_at,
                    end_at=end_at,
                    court=fixed.court,
                )
                .select_related("coach", "substitute_coach", "court")
                .order_by("id")
                .first()
            )

            if selected_coach is not None and not (
                legacy._fixed_lesson_includes_coach(fixed, selected_coach)
                or (
                    availability
                    and availability.substitute_coach_id == selected_coach.pk
                )
            ):
                continue

            reservations = list(
                Reservation.objects.filter(
                    fixed_lesson=fixed,
                    start_at=start_at,
                    end_at=end_at,
                    status=Reservation.STATUS_ACTIVE,
                )
                .select_related("user", "coach", "substitute_coach", "court")
                .order_by("user__full_name", "user__username", "id")
            )
            reservation_names = [item.user.display_name() for item in reservations]
            waitlist_count = LessonWaitlist.objects.filter(
                fixed_lesson=fixed,
                start_at=start_at,
                end_at=end_at,
                status=LessonWaitlist.STATUS_WAITING,
            ).count()

            assigned_coach = (
                availability.substitute_coach
                if availability and availability.substitute_coach
                else fixed.primary_coach()
                if hasattr(fixed, "primary_coach")
                else fixed.coach
            )

            rows.append(
                {
                    "fixed_lesson": fixed,
                    "weekday_label": weekday_labels.get(
                        fixed.weekday, str(fixed.weekday)
                    ),
                    "target_date": target_date,
                    "start_at": start_at,
                    "end_at": end_at,
                    "assigned_coach_name": legacy._display_name(assigned_coach),
                    "normal_coach_name": legacy._fixed_lesson_coach_names(fixed),
                    "substitute_coach_name": (
                        legacy._display_name(availability.substitute_coach)
                        if availability and availability.substitute_coach
                        else ""
                    ),
                    "has_substitute": bool(
                        availability and availability.substitute_coach
                    ),
                    "member_count": len(reservations),
                    "member_names": reservation_names,
                    "reservation_count": len(reservations),
                    "reservation_names": reservation_names,
                    "waitlist_count": waitlist_count,
                    "capacity": (
                        fixed.effective_capacity()
                        if hasattr(fixed, "effective_capacity")
                        else fixed.capacity
                    ),
                    "slot_availability": availability,
                }
            )

    rows.sort(
        key=lambda row: (
            row["target_date"],
            row["start_at"],
            row["fixed_lesson"].id,
        )
    )

    return render(
        request,
        "coach/fixed_lesson_weekly.html",
        {
            "coach_options": coach_queryset,
            "selected_coach": selected_coach,
            "selected_coach_id": selected_coach_id,
            "fixed_lessons": rows,
            "week_start": today,
            "week_end": display_until,
            "week_label": f"{today:%Y-%m-%d} 〜 {display_until:%Y-%m-%d}",
            "display_weeks": display_weeks,
            "is_staff_mode": is_staff_mode,
        },
    )
