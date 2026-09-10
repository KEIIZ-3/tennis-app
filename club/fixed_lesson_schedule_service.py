from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from .lesson_participants import CAPACITY_CONSUMING_STATUSES
from .models import CoachAvailability, FixedLesson, LessonWaitlist, Reservation


def _occurrence_datetimes(fixed_lesson, reference_date):
    return {
        fixed_lesson._build_datetimes_for_date(target_date)
        for target_date in fixed_lesson.scheduled_occurrence_dates()
        if target_date >= reference_date
    }


def schedule_difference(current, proposed, *, reference_date=None):
    """Return future physical occurrences removed, added, and retained."""
    reference_date = reference_date or timezone.localdate()
    old_occurrences = _occurrence_datetimes(current, reference_date)
    new_occurrences = _occurrence_datetimes(proposed, reference_date)
    return {
        "removed": old_occurrences - new_occurrences,
        "added": new_occurrences - old_occurrences,
        "unchanged": old_occurrences & new_occurrences,
    }


def _blocking_occurrences(fixed_lesson, removed):
    blocked = []
    for start_at, end_at in sorted(removed):
        occurrence_filter = models.Q(
            fixed_lesson=fixed_lesson,
            start_at=start_at,
            end_at=end_at,
        ) | models.Q(
            availability__fixed_lesson_source=fixed_lesson,
            start_at=start_at,
            end_at=end_at,
        )
        reservation_count = Reservation.objects.filter(
            occurrence_filter,
            status__in=CAPACITY_CONSUMING_STATUSES,
        ).count()
        waitlist_count = LessonWaitlist.objects.filter(
            occurrence_filter,
            status=LessonWaitlist.STATUS_WAITING,
        ).count()
        if reservation_count or waitlist_count:
            blocked.append((start_at, reservation_count, waitlist_count))
    return blocked


def _blocking_message(blocked):
    details = []
    for start_at, reservations, waitlists in blocked[:5]:
        local_start = timezone.localtime(start_at) if timezone.is_aware(start_at) else start_at
        counts = []
        if reservations:
            counts.append(f"予約{reservations}件")
        if waitlists:
            counts.append(f"キャンセル待ち{waitlists}件")
        details.append(f"{local_start:%Y/%m/%d %H:%M}（{'・'.join(counts)}）")
    suffix = f"、ほか{len(blocked) - 5}開催回" if len(blocked) > 5 else ""
    return (
        "既存予約/キャンセル待ちがあるため日程を変更できません。"
        f"対象: {'、'.join(details)}{suffix}。先に該当予約を整理してください。"
    )


def validate_fixed_lesson_schedule_change(proposed):
    """Validate an admin candidate against the persisted schedule."""
    if not proposed.pk:
        return {"removed": set(), "added": set(), "unchanged": set()}
    current = FixedLesson.objects.get(pk=proposed.pk)
    difference = schedule_difference(current, proposed)
    blocked = _blocking_occurrences(current, difference["removed"])
    if blocked:
        raise ValidationError(_blocking_message(blocked))
    return difference


def _has_history(availability):
    return (
        availability.reservations.exists()
        or availability.lesson_waitlists.exists()
        or hasattr(availability, "completed_registration")
        or availability.rain_refunds.exists()
    )


def _remove_unused_occurrences(fixed_lesson, removed):
    for start_at, end_at in removed:
        availabilities = CoachAvailability.objects.select_for_update().filter(
            fixed_lesson_source=fixed_lesson,
            start_at=start_at,
            end_at=end_at,
        )
        for availability in availabilities:
            if not _has_history(availability):
                availability.delete()


def update_fixed_lesson_schedule(proposed, *, actor=None):
    """Persist an existing FixedLesson and migrate its future schedule atomically."""
    if not proposed.pk:
        raise ValueError("保存済みの固定レッスンを指定してください。")

    from .fixed_lesson_sync_facade import synchronize_fixed_lesson_membership

    with transaction.atomic():
        current = FixedLesson.objects.select_for_update().get(pk=proposed.pk)
        difference = schedule_difference(current, proposed)
        blocked = _blocking_occurrences(current, difference["removed"])
        if blocked:
            raise ValidationError(_blocking_message(blocked))

        concrete_fields = [
            field.name for field in FixedLesson._meta.concrete_fields if not field.primary_key
        ]
        for field_name in concrete_fields:
            setattr(current, field_name, getattr(proposed, field_name))
        current.save(update_fields=concrete_fields)

        if difference["removed"] or difference["added"]:
            _remove_unused_occurrences(current, difference["removed"])
            synchronize_fixed_lesson_membership(current.pk, created_by=actor)

        for field_name in concrete_fields:
            setattr(proposed, field_name, getattr(current, field_name))
        return difference
