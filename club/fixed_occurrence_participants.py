from datetime import date

from django.utils import timezone

from .models import FixedLesson, Reservation


def occurrence_key(fixed_lesson_id, target_date):
    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)
    return str(fixed_lesson_id), target_date.isoformat()


def active_reservations_for_occurrence(fixed_lesson, target_date):
    """固定開催回の実参加者を返す。

    FixedLesson.members は将来予約生成用の設定であり、開催回の人数には使わない。
    また、同じコーチ・コート・日時という物理枠だけの一致も使わない。
    開催回へ明示的に紐づく有効 Reservation だけを正本とする。
    """
    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)
    start_at, end_at = fixed_lesson._build_datetimes_for_date(target_date)
    return Reservation.objects.filter(
        fixed_lesson=fixed_lesson,
        start_at=start_at,
        end_at=end_at,
        status=Reservation.STATUS_ACTIVE,
    )


def active_count_for_occurrence(fixed_lesson, target_date):
    return active_reservations_for_occurrence(fixed_lesson, target_date).count()


def active_count_map_for_month(year, month):
    result = {}
    fixed_lessons = FixedLesson.objects.filter(is_active=True).order_by("id")
    for fixed_lesson in fixed_lessons:
        dates = fixed_lesson.scheduled_occurrence_dates()
        for target_date in dates:
            if target_date.year != year or target_date.month != month:
                continue
            result[occurrence_key(fixed_lesson.pk, target_date)] = (
                active_count_for_occurrence(fixed_lesson, target_date)
            )
    return result
