RECONCILE_MATCH = "match"
RECONCILE_COHERENT_OVERRIDE = "coherent_override"
RECONCILE_LEGACY_MISSING_COACH = "legacy_missing_coach"
RECONCILE_AMBIGUOUS_ASSIGNMENT = "ambiguous_assignment"


def classify_fixed_lesson_coach_assignment(
    *,
    availability_coach_id,
    availability_coach_2_id,
    availability_coach_count,
    fixed_lesson_coach_id,
    fixed_lesson_coach_2_id,
    fixed_lesson_coach_count,
):
    """Classify legacy coach data without depending on Django model classes."""
    available_coach_count = 1 + int(availability_coach_2_id is not None)
    matches_source = (
        availability_coach_id == fixed_lesson_coach_id
        and availability_coach_2_id == fixed_lesson_coach_2_id
        and availability_coach_count == fixed_lesson_coach_count
    )
    if matches_source:
        return RECONCILE_MATCH
    if availability_coach_count == available_coach_count:
        return RECONCILE_COHERENT_OVERRIDE
    if (
        availability_coach_count > available_coach_count
        and fixed_lesson_coach_count == availability_coach_count
    ):
        return RECONCILE_LEGACY_MISSING_COACH
    return RECONCILE_AMBIGUOUS_ASSIGNMENT
