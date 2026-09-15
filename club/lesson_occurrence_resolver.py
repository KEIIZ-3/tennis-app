from django.utils import timezone

from .models import FixedLesson, Reservation


LEGACY_FIXED_LESSON_NOTE_PREFIX = "固定レッスン:"


def _local(value):
    if value is not None and timezone.is_aware(value):
        return timezone.localtime(value)
    return value


def _legacy_title(availability):
    note = (getattr(availability, "note", "") or "").strip()
    if not note.startswith(LEGACY_FIXED_LESSON_NOTE_PREFIX):
        return ""
    return note[len(LEGACY_FIXED_LESSON_NOTE_PREFIX):].strip()


def _legacy_candidate_matches(availability, fixed_lesson, legacy_title):
    local_start = _local(availability.start_at)
    local_end = _local(availability.end_at)
    target_date = local_start.date()
    if not fixed_lesson.is_active or fixed_lesson.lesson_type != availability.lesson_type:
        return False
    if (fixed_lesson.title or "").strip() != legacy_title:
        return False
    if fixed_lesson.target_level != availability.target_level:
        return False
    if (fixed_lesson.target_level_2 or "") != (availability.target_level_2 or ""):
        return False
    if fixed_lesson.court_id and fixed_lesson.court_id != availability.court_id:
        return False
    try:
        if target_date not in set(fixed_lesson.scheduled_occurrence_dates()):
            return False
        fixed_start, fixed_end = fixed_lesson._build_datetimes_for_date(target_date)
    except Exception:
        return False
    return _local(fixed_start) == local_start and _local(fixed_end) == local_end


def resolve_authoritative_fixed_lesson(
    availability,
    *,
    reservations=None,
    fixed_lessons=None,
):
    """Resolve an availability occurrence only from explicit or unique legacy identity."""
    if availability is None:
        return None

    if reservations is None:
        reservations = (
            Reservation.objects.filter(
                availability=availability,
                start_at=availability.start_at,
                end_at=availability.end_at,
                fixed_lesson_id__isnull=False,
            )
            .select_related("fixed_lesson")
            .order_by("id")
        )
    explicit = {
        reservation.fixed_lesson_id: reservation.fixed_lesson
        for reservation in reservations
        if reservation.availability_id == availability.pk
        and reservation.fixed_lesson_id
        and reservation.start_at == availability.start_at
        and reservation.end_at == availability.end_at
    }
    if len(explicit) == 1:
        return next(iter(explicit.values()))
    if len(explicit) > 1:
        return None

    if availability.fixed_lesson_source_id:
        return availability.fixed_lesson_source

    legacy_title = _legacy_title(availability)
    if not legacy_title:
        return None

    if fixed_lessons is None:
        fixed_lessons = FixedLesson.objects.filter(
            is_active=True,
            lesson_type=availability.lesson_type,
            title=legacy_title,
        ).select_related("coach", "coach_2", "coach_3", "court")
    matches = [
        fixed_lesson
        for fixed_lesson in fixed_lessons
        if _legacy_candidate_matches(availability, fixed_lesson, legacy_title)
    ]
    return matches[0] if len(matches) == 1 else None
