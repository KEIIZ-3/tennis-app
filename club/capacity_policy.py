from datetime import date

from django.utils import timezone

CAPACITY_RULE_CHANGE_DATE = date(2026, 8, 1)
GENERAL_CAPACITY_PER_COACH_BEFORE_CHANGE = 6
GENERAL_CAPACITY_PER_COACH_FROM_CHANGE = 5


def _local_date(value):
    if value is None:
        return None
    if hasattr(value, "date"):
        try:
            if timezone.is_aware(value):
                value = timezone.localtime(value)
            return value.date()
        except Exception:
            return value.date()
    return value


def general_capacity_per_coach(target_date):
    lesson_date = _local_date(target_date)
    if lesson_date and lesson_date >= CAPACITY_RULE_CHANGE_DATE:
        return GENERAL_CAPACITY_PER_COACH_FROM_CHANGE
    return GENERAL_CAPACITY_PER_COACH_BEFORE_CHANGE


def general_lesson_capacity(coach_count, target_date):
    normalized_coach_count = max(int(coach_count or 1), 1)
    return normalized_coach_count * general_capacity_per_coach(target_date)
