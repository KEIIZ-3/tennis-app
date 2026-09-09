from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from .lesson_participants import CAPACITY_CONSUMING_STATUSES
from .fixed_lesson_provenance import (
    RECONCILE_AMBIGUOUS_ASSIGNMENT,
    RECONCILE_COHERENT_OVERRIDE,
    classify_fixed_lesson_coach_assignment,
)
from .models import (
    CoachAvailability,
    FixedLesson,
    FixedLessonCanceledOccurrence,
    LessonWaitlist,
    Reservation,
)


CANCELLATION_REASON = "固定レッスン開催回の中止"

def _linked_fixed_lesson_ids(availability_id):
    return list(
        Reservation.objects.filter(
            availability_id=availability_id,
            fixed_lesson_id__isnull=False,
        )
        .order_by("fixed_lesson_id")
        .values_list("fixed_lesson_id", flat=True)
        .distinct()[:2]
    )


def reconcile_fixed_lesson_availability(availability, fixed_lesson):
    """Atomically establish provenance and its coach-assignment invariant.

    A caller may authoritatively supply an as-yet unlinked generated occurrence.
    Once reservation links exist, they must identify exactly the supplied lesson.
    Conflicting or ambiguous provenance is reported without changing the row.
    """
    if availability.pk is None or fixed_lesson.pk is None:
        raise ValueError("保存済みの開催枠と固定レッスンを指定してください。")

    with transaction.atomic():
        locked = CoachAvailability.objects.select_for_update().get(pk=availability.pk)
        linked_ids = _linked_fixed_lesson_ids(locked.pk)
        if linked_ids and linked_ids != [fixed_lesson.pk]:
            return {"status": "ambiguous_relation", "changed_fields": []}
        if locked.fixed_lesson_source_id not in (None, fixed_lesson.pk):
            return {"status": "source_conflict", "changed_fields": []}
        target_date = timezone.localtime(locked.start_at).date()
        expected_start, expected_end = fixed_lesson._build_datetimes_for_date(target_date)
        if (
            locked.start_at != expected_start
            or locked.end_at != expected_end
            or locked.lesson_type != fixed_lesson.lesson_type
        ):
            return {"status": "occurrence_mismatch", "changed_fields": []}

        changed_fields = []
        classification = None
        if locked.fixed_lesson_source_id is None:
            classification = classify_fixed_lesson_coach_assignment(
                availability_coach_id=locked.coach_id,
                availability_coach_2_id=locked.coach_2_id,
                availability_coach_count=locked.coach_count,
                fixed_lesson_coach_id=fixed_lesson.coach_id,
                fixed_lesson_coach_2_id=fixed_lesson.coach_2_id,
                fixed_lesson_coach_count=fixed_lesson.coach_count,
            )
            locked.fixed_lesson_source = fixed_lesson
            changed_fields.append("fixed_lesson_source")
            should_override = classification in {
                RECONCILE_COHERENT_OVERRIDE,
                RECONCILE_AMBIGUOUS_ASSIGNMENT,
            }
            if locked.coach_assignment_overridden != should_override:
                locked.coach_assignment_overridden = should_override
                changed_fields.append("coach_assignment_overridden")

        if not locked.coach_assignment_overridden:
            inherited_coach_count = fixed_lesson.coach_count
            if fixed_lesson.lesson_type == FixedLesson.LESSON_GENERAL:
                inherited_coach_count = 1 + int(fixed_lesson.coach_2_id is not None)
            expected = {
                "coach_id": fixed_lesson.coach_id,
                "coach_2_id": fixed_lesson.coach_2_id,
                "coach_count": inherited_coach_count,
            }
            for field_name, value in expected.items():
                if getattr(locked, field_name) != value:
                    setattr(locked, field_name, value)
                    changed_fields.append(field_name.removesuffix("_id"))

        if changed_fields:
            update_values = {
                field_name: getattr(locked, field_name)
                for field_name in dict.fromkeys(changed_fields)
            }
            CoachAvailability.objects.filter(pk=locked.pk).update(**update_values)
        availability.refresh_from_db()
        return {
            "status": classification or "reconciled",
            "changed_fields": list(dict.fromkeys(changed_fields)),
        }


def reconcile_future_unclassified_availabilities(*, reference_date=None):
    """Repair safe future rows and report relations that cannot be classified."""
    reference_date = reference_date or timezone.localdate()
    candidate_ids = list(
        Reservation.objects.filter(
            availability_id__isnull=False,
            fixed_lesson_id__isnull=False,
            availability__fixed_lesson_source__isnull=True,
            availability__start_at__date__gte=reference_date,
        )
        .order_by("availability_id")
        .values_list("availability_id", flat=True)
        .distinct()
    )
    result = {"candidates": len(candidate_ids), "reconciled": 0, "ambiguous": 0}
    for availability_id in candidate_ids:
        fixed_ids = _linked_fixed_lesson_ids(availability_id)
        if len(fixed_ids) != 1:
            result["ambiguous"] += 1
            continue
        availability = CoachAvailability.objects.get(pk=availability_id)
        fixed_lesson = FixedLesson.objects.get(pk=fixed_ids[0])
        outcome = reconcile_fixed_lesson_availability(availability, fixed_lesson)
        if outcome["status"] in {"ambiguous_relation", "source_conflict", "occurrence_mismatch"}:
            result["ambiguous"] += 1
        else:
            result["reconciled"] += 1
    remaining_ids = list(CoachAvailability.objects.filter(
        pk__in=candidate_ids,
        fixed_lesson_source__isnull=True,
    ).values_list("pk", flat=True))
    result["remaining_unclassified"] = len(remaining_ids)
    result["remaining_unique"] = sum(
        len(_linked_fixed_lesson_ids(availability_id)) == 1
        for availability_id in remaining_ids
    )
    return result


def _candidate_fixed_lessons(availability):
    target_date = timezone.localtime(availability.start_at).date()
    candidates = []
    queryset = FixedLesson.objects.select_for_update().filter(
        is_active=True,
        coach=availability.coach,
        court=availability.court,
        lesson_type=availability.lesson_type,
        start_hour=timezone.localtime(availability.start_at).hour,
    )
    for fixed_lesson in queryset.order_by("pk"):
        if target_date not in fixed_lesson.configured_occurrence_dates():
            continue
        start_at, end_at = fixed_lesson._build_datetimes_for_date(target_date)
        if start_at == availability.start_at and end_at == availability.end_at:
            candidates.append(fixed_lesson)
    return target_date, candidates


def _fixed_lesson_for_availability(availability):
    linked_ids = set(
        Reservation.objects.filter(availability=availability, fixed_lesson_id__isnull=False)
        .values_list("fixed_lesson_id", flat=True)
    )
    linked_ids.update(
        LessonWaitlist.objects.filter(availability=availability, fixed_lesson_id__isnull=False)
        .values_list("fixed_lesson_id", flat=True)
    )
    target_date, candidates = _candidate_fixed_lessons(availability)
    if linked_ids:
        linked_candidates = [item for item in candidates if item.pk in linked_ids]
        if len(linked_candidates) == 1:
            return target_date, linked_candidates[0]
        raise ValidationError("開催枠に複数の固定レッスンが紐づいているため削除できません。")
    if len(candidates) == 1:
        return target_date, candidates[0]
    if len(candidates) > 1:
        raise ValidationError("同日時に複数の固定レッスン候補があるため削除できません。")
    return target_date, None


def delete_or_cancel_availability(*, availability_id, actor=None):
    """通常枠は削除し、固定レッスン由来ならその開催回だけを中止する。"""
    with transaction.atomic():
        availability = CoachAvailability.objects.select_for_update().get(pk=availability_id)
        target_date, fixed_lesson = _fixed_lesson_for_availability(availability)
        if fixed_lesson is None:
            availability.delete()
            return False

        FixedLessonCanceledOccurrence.objects.get_or_create(
            fixed_lesson=fixed_lesson,
            occurrence_date=target_date,
            defaults={"canceled_by": actor if getattr(actor, "pk", None) else None},
        )
        start_at, end_at = fixed_lesson._build_datetimes_for_date(target_date)
        reservations = Reservation.objects.select_for_update().filter(
            models.Q(fixed_lesson=fixed_lesson) | models.Q(availability=availability),
            start_at=start_at,
            end_at=end_at,
            status__in=CAPACITY_CONSUMING_STATUSES,
        ).order_by("pk")
        for reservation in reservations:
            reservation.cancel(created_by=actor, reason=CANCELLATION_REASON)

        waitlists = LessonWaitlist.objects.select_for_update().filter(
            models.Q(fixed_lesson=fixed_lesson) | models.Q(availability=availability),
            start_at=start_at,
            end_at=end_at,
            status=LessonWaitlist.STATUS_WAITING,
        ).order_by("pk")
        for waitlist in waitlists:
            waitlist.cancel(reason=CANCELLATION_REASON)

        availability.delete()
        return True
