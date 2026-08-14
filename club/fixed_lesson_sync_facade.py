from contextlib import contextmanager
from contextvars import ContextVar

from django.core.exceptions import ValidationError
from django.db import transaction

from .fixed_lesson_integrity_service import (
    UNASSIGNED_COURT_NAME,
    _locked_occurrence_reservations,
    configured_future_dates,
    synchronize_fixed_lesson_membership,
)
from .models import FixedLesson, User


_membership_signal_suppression_depth = ContextVar(
    "fixed_lesson_membership_signal_suppression_depth",
    default=0,
)


def membership_signal_is_suppressed():
    return _membership_signal_suppression_depth.get() > 0


@contextmanager
def suppress_membership_signal_sync():
    """正本service内のM2M更新をsignalの二重同期から隔離する。"""
    token = _membership_signal_suppression_depth.set(
        _membership_signal_suppression_depth.get() + 1
    )
    try:
        yield
    finally:
        _membership_signal_suppression_depth.reset(token)


def replace_fixed_lesson_members(fixed_lesson, members, created_by=None):
    """固定メンバー集合と将来予約を、1回の業務操作として原子的に置換する。"""
    if not fixed_lesson.pk:
        raise ValueError("保存済みの固定レッスンを指定してください。")

    requested_ids = set()
    for member in members:
        member_id = member.pk if isinstance(member, User) else member
        if member_id is None:
            raise ValueError("未保存のユーザーは固定メンバーに指定できません。")
        requested_ids.add(int(member_id))

    with transaction.atomic():
        locked_lesson = FixedLesson.objects.select_for_update().get(pk=fixed_lesson.pk)
        valid_ids = set(
            User.objects.filter(
                pk__in=requested_ids,
                role__in=User.LESSON_PARTICIPANT_ROLE_VALUES,
            ).values_list("pk", flat=True)
        )
        invalid_ids = requested_ids - valid_ids
        if invalid_ids:
            raise ValidationError(
                "固定メンバーに指定できないユーザーです: "
                + ", ".join(str(pk) for pk in sorted(invalid_ids))
            )

        current_ids = set(locked_lesson.members.values_list("pk", flat=True))
        added_ids = valid_ids - current_ids
        removed_ids = current_ids - valid_ids
        if added_ids or removed_ids:
            with suppress_membership_signal_sync():
                locked_lesson.members.set(sorted(valid_ids))

        changed_count = synchronize_fixed_lesson_membership(
            locked_lesson.pk,
            created_by=created_by,
        )
        return {
            "added_ids": added_ids,
            "removed_ids": removed_ids,
            "changed_count": changed_count,
        }


def synchronize_fixed_lesson(fixed_lesson_id, created_by=None):
    """Synchronize one fixed lesson through the canonical membership service."""
    return synchronize_fixed_lesson_membership(
        fixed_lesson_id,
        created_by=created_by,
    )


def set_fixed_lesson_activity(fixed_lesson_id, *, is_active, created_by=None):
    """Change activity and synchronize reservations in one transaction."""
    with transaction.atomic():
        fixed_lesson = FixedLesson.objects.select_for_update().get(
            pk=fixed_lesson_id
        )
        changed = fixed_lesson.is_active != is_active
        if changed:
            fixed_lesson.is_active = is_active
            fixed_lesson.save(update_fields=["is_active"])

        synchronized_count = synchronize_fixed_lesson_membership(
            fixed_lesson.pk,
            created_by=created_by,
        )
        return {
            "changed": changed,
            "synchronized_count": synchronized_count,
        }

_locked_active_occurrence_reservations = _locked_occurrence_reservations


__all__ = [
    "UNASSIGNED_COURT_NAME",
    "_locked_active_occurrence_reservations",
    "configured_future_dates",
    "membership_signal_is_suppressed",
    "replace_fixed_lesson_members",
    "set_fixed_lesson_activity",
    "synchronize_fixed_lesson",
    "synchronize_fixed_lesson_membership",
]
