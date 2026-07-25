from django.db import transaction

from .fixed_lesson_membership_service import (
    synchronize_fixed_lesson_membership as synchronize_fixed_lesson_membership_core,
)
from .models import Court, FixedLesson


UNASSIGNED_COURT_NAME = "コート未定（後日決定）"


def _ensure_booking_court(fixed_lesson_id):
    """コート未定の固定レッスンにも、予約を表現できる正式な仮コートを割り当てる。"""
    fixed_lesson = (
        FixedLesson.objects.select_for_update()
        .select_related("court")
        .get(pk=fixed_lesson_id)
    )
    if fixed_lesson.court_id:
        return fixed_lesson.court

    placeholder_court, _created = Court.objects.get_or_create(
        name=UNASSIGNED_COURT_NAME,
        defaults={
            "is_active": True,
            "court_type": Court.COURT_OTHER,
        },
    )
    update_fields = []
    if not placeholder_court.is_active:
        placeholder_court.is_active = True
        update_fields.append("is_active")
    if placeholder_court.court_type != Court.COURT_OTHER:
        placeholder_court.court_type = Court.COURT_OTHER
        update_fields.append("court_type")
    if update_fields:
        placeholder_court.save(update_fields=update_fields)

    fixed_lesson.court = placeholder_court
    fixed_lesson.save(update_fields=["court"])
    return placeholder_court


def synchronize_fixed_lesson_membership(fixed_lesson_id, created_by=None):
    """固定メンバー同期の全入口。

    Reservation と CoachAvailability はコート必須の既存設計なので、
    コート未定を「同期対象外」にせず、正式な仮コートとして表現してから
    既存の原子的な同期サービスへ渡す。
    """
    with transaction.atomic():
        _ensure_booking_court(fixed_lesson_id)
        return synchronize_fixed_lesson_membership_core(
            fixed_lesson_id,
            created_by=created_by,
        )
