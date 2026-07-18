from . import lesson_execution
from .models import CoachAvailability


def _canonical_availability_for_fixed(fixed_lesson, start_at, end_at):
    """
    レッスンカレンダーと開催管理で、固定レッスンの参照元を統一する。

    FixedLesson を正規データとして扱い、対応する CoachAvailability が
    存在しない場合は、その場で1件だけ自動補完する。
    担当変更前の古い枠は検索対象に含めない。
    """
    primary_coach = (
        fixed_lesson.primary_coach()
        if hasattr(fixed_lesson, "primary_coach")
        else fixed_lesson.coach
    )

    if primary_coach is None or fixed_lesson.court_id is None:
        return None

    defaults = {
        "capacity": max(int(fixed_lesson.effective_capacity() or 1), 1),
        "coach_count": max(int(fixed_lesson.coach_count or 1), 1),
        "court_count": max(int(fixed_lesson.court_count or 1), 1),
        "target_level": fixed_lesson.target_level,
        "target_level_2": getattr(fixed_lesson, "target_level_2", "") or "",
        "status": CoachAvailability.STATUS_OPEN,
        "note": f"固定レッスン: {fixed_lesson.title or fixed_lesson.get_weekday_display()}",
    }

    availability, _created = CoachAvailability.objects.get_or_create(
        coach=primary_coach,
        court=fixed_lesson.court,
        lesson_type=fixed_lesson.lesson_type,
        start_at=start_at,
        end_at=end_at,
        defaults=defaults,
    )

    update_fields = []
    for field_name, expected_value in defaults.items():
        if getattr(availability, field_name) != expected_value:
            setattr(availability, field_name, expected_value)
            update_fields.append(field_name)

    if update_fields:
        availability.save(update_fields=update_fields)

    return availability


lesson_execution._canonical_availability_for_fixed = (
    _canonical_availability_for_fixed
)
