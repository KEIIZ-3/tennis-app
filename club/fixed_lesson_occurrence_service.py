from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from .lesson_participants import CAPACITY_CONSUMING_STATUSES
from .models import (
    CoachAvailability,
    FixedLesson,
    FixedLessonCanceledOccurrence,
    LessonWaitlist,
    Reservation,
)


CANCELLATION_REASON = "固定レッスン開催回の中止"


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
