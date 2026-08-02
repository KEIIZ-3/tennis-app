import logging
from datetime import date

from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger("club.akagi_reservation_diagnostic")

TARGET_NAME = "赤木琴江"


def _local_text(value):
    if not value:
        return "-"
    try:
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)


def _display_name(user):
    if not user:
        return "-"
    try:
        return user.display_name()
    except Exception:
        return getattr(user, "username", "-") or "-"


def _reservation_rows_for_user(user):
    from .models import Reservation

    return list(
        Reservation.objects.filter(user=user)
        .filter(Q(fixed_lesson__isnull=False) | Q(is_fixed_entry=True))
        .select_related(
            "fixed_lesson",
            "coach",
            "substitute_coach",
            "court",
            "availability",
        )
        .order_by("start_at", "id")
    )


def _matching_fixed_lessons(user):
    from .models import FixedLesson

    return list(
        FixedLesson.objects.filter(
            members=user,
            weekday=6,
            start_hour=19,
        )
        .select_related("coach", "coach_2", "coach_3", "court")
        .order_by("start_date", "id")
    )


def emit_akagi_reservation_diagnostic():
    """本番データを変更せず、赤木琴江さんの固定予約状況だけをログ出力する。"""
    from .models import User

    candidates = list(
        User.objects.filter(
            Q(full_name__icontains=TARGET_NAME)
            | Q(username__icontains=TARGET_NAME)
            | Q(first_name__icontains=TARGET_NAME)
            | Q(last_name__icontains=TARGET_NAME)
        ).order_by("id")
    )

    logger.warning("[AKAGI_DIAG] START target=%s candidate_count=%s", TARGET_NAME, len(candidates))

    if not candidates:
        logger.warning("[AKAGI_DIAG] USER_NOT_FOUND target=%s", TARGET_NAME)
        logger.warning("[AKAGI_DIAG] END")
        return

    for user in candidates:
        logger.warning(
            "[AKAGI_DIAG] USER id=%s username=%s full_name=%s role=%s",
            user.pk,
            getattr(user, "username", ""),
            getattr(user, "full_name", ""),
            getattr(user, "role", ""),
        )

        fixed_lessons = _matching_fixed_lessons(user)
        logger.warning(
            "[AKAGI_DIAG] FIXED_LESSON_COUNT user_id=%s count=%s",
            user.pk,
            len(fixed_lessons),
        )

        for lesson in fixed_lessons:
            scheduled_dates = []
            try:
                scheduled_dates = lesson.scheduled_occurrence_dates()
            except Exception as exc:
                logger.warning(
                    "[AKAGI_DIAG] FIXED_LESSON_SCHEDULE_ERROR lesson_id=%s error=%s",
                    lesson.pk,
                    exc,
                )

            logger.warning(
                "[AKAGI_DIAG] FIXED_LESSON id=%s title=%s active=%s start_date=%s weekday=%s start_hour=%s weeks_ahead=%s court=%s coaches=%s scheduled_dates=%s",
                lesson.pk,
                lesson.title or "-",
                lesson.is_active,
                lesson.start_date,
                lesson.weekday,
                lesson.start_hour,
                lesson.weeks_ahead,
                lesson.court_display(),
                lesson.coach_display_names(),
                ",".join(str(value) for value in scheduled_dates) or "-",
            )

        reservations = _reservation_rows_for_user(user)
        logger.warning(
            "[AKAGI_DIAG] RESERVATION_COUNT user_id=%s count=%s",
            user.pk,
            len(reservations),
        )

        for reservation in reservations:
            fixed_lesson = getattr(reservation, "fixed_lesson", None)
            start_local = reservation.start_at
            if start_local and timezone.is_aware(start_local):
                start_local = timezone.localtime(start_local)

            is_sunday_19 = bool(
                start_local
                and start_local.weekday() == 6
                and start_local.hour == 19
            )

            logger.warning(
                "[AKAGI_DIAG] RESERVATION id=%s fixed_lesson_id=%s fixed_title=%s is_fixed_entry=%s sunday_19_21=%s start=%s end=%s status=%s canceled_at=%s cancellation_reason=%s coach=%s substitute=%s court=%s availability_id=%s created_at=%s",
                reservation.pk,
                getattr(reservation, "fixed_lesson_id", None),
                getattr(fixed_lesson, "title", "") or "-",
                getattr(reservation, "is_fixed_entry", False),
                is_sunday_19,
                _local_text(getattr(reservation, "start_at", None)),
                _local_text(getattr(reservation, "end_at", None)),
                getattr(reservation, "status", ""),
                _local_text(getattr(reservation, "canceled_at", None)),
                getattr(reservation, "cancellation_reason", "") or "-",
                _display_name(getattr(reservation, "coach", None)),
                _display_name(getattr(reservation, "substitute_coach", None)),
                str(getattr(reservation, "court", None) or "-"),
                getattr(reservation, "availability_id", None),
                _local_text(getattr(reservation, "created_at", None)),
            )

        today = timezone.localdate()
        logger.warning(
            "[AKAGI_DIAG] SUMMARY user_id=%s today=%s active_future=%s canceled_future=%s rain_canceled_future=%s",
            user.pk,
            today,
            sum(1 for row in reservations if getattr(row, "status", "") == "active" and getattr(row, "start_at", None) and row.start_at.date() >= today),
            sum(1 for row in reservations if getattr(row, "status", "") == "canceled" and getattr(row, "start_at", None) and row.start_at.date() >= today),
            sum(1 for row in reservations if getattr(row, "status", "") == "rain_canceled" and getattr(row, "start_at", None) and row.start_at.date() >= today),
        )

    logger.warning("[AKAGI_DIAG] END")


try:
    emit_akagi_reservation_diagnostic()
except Exception:
    logger.exception("[AKAGI_DIAG] FAILED")
