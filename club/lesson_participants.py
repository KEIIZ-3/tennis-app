from datetime import date

from .models import Reservation


ACTIVE_PARTICIPANT_STATUSES = (Reservation.STATUS_ACTIVE,)


def reservations_for_lesson(*, fixed_lesson=None, availability=None, coach=None,
                            court=None, lesson_type=None, start_at=None, end_at=None,
                            statuses=ACTIVE_PARTICIPANT_STATUSES):
    """Return canonical Reservation rows for one lesson occurrence.

    Reservation is the attendance source of truth. FixedLesson.members is only
    recurring configuration and must not be counted for an individual date.
    """
    if fixed_lesson is not None and (start_at is None or end_at is None):
        raise ValueError("固定レッスンの参加者取得には開催日時が必要です。")

    queryset = Reservation.objects.filter(status__in=statuses)
    filters = {"lesson_type": lesson_type, "start_at": start_at, "end_at": end_at}
    queryset = queryset.filter(**{key: value for key, value in filters.items() if value is not None})

    # Stable relation identifiers take precedence. Falling back to physical
    # fields is only for legacy reservations without either relation; OR-ing
    # every hint would merge two distinct lessons that share a court/time.
    if fixed_lesson is not None:
        queryset = queryset.filter(fixed_lesson=fixed_lesson)
    elif availability is not None:
        queryset = queryset.filter(availability=availability)
    elif coach is not None and court is not None:
        queryset = queryset.filter(coach=coach, court=court)
    elif coach is not None:
        queryset = queryset.filter(coach=coach)
    elif court is not None:
        queryset = queryset.filter(court=court)

    return queryset.order_by("user__full_name", "user__username", "id").distinct()


def reservations_for_object(obj, *, statuses=ACTIVE_PARTICIPANT_STATUSES):
    return reservations_for_lesson(
        fixed_lesson=getattr(obj, "fixed_lesson", None),
        availability=getattr(obj, "availability", None),
        coach=getattr(obj, "coach", None),
        court=getattr(obj, "court", None),
        lesson_type=getattr(obj, "lesson_type", None),
        start_at=getattr(obj, "start_at", None),
        end_at=getattr(obj, "end_at", None),
        statuses=statuses,
    )


def reservations_for_fixed_occurrence(fixed_lesson, target_date, *, statuses=ACTIVE_PARTICIPANT_STATUSES):
    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)
    start_at, end_at = fixed_lesson._build_datetimes_for_date(target_date)
    return reservations_for_lesson(
        fixed_lesson=fixed_lesson, coach=fixed_lesson.primary_coach(),
        court=fixed_lesson.court, lesson_type=fixed_lesson.lesson_type,
        start_at=start_at, end_at=end_at, statuses=statuses,
    )
