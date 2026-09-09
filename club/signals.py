import logging

from django.db.models.signals import m2m_changed, post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from .fixed_lesson_sync_facade import (
    membership_signal_is_suppressed,
    synchronize_fixed_lesson_membership,
)
from .models import FixedLesson, Reservation
from .reservation_notification_service import schedule_reservation_canceled_notification

logger = logging.getLogger(__name__)


@receiver(
    post_save,
    sender=Reservation,
    dispatch_uid="club.reservation_fixed_lesson_availability_provenance",
    weak=False,
)
def reservation_fixed_lesson_availability_provenance(
    sender, instance, raw=False, **kwargs
):
    """Safety net for reservation writers outside the canonical service."""
    if raw or not instance.availability_id or not instance.fixed_lesson_id:
        return
    from .fixed_lesson_occurrence_service import reconcile_fixed_lesson_availability

    reconcile_fixed_lesson_availability(instance.availability, instance.fixed_lesson)


@receiver(pre_save, sender=FixedLesson, dispatch_uid="club.fixed_lesson_store_old_coaches", weak=False)
def fixed_lesson_store_old_coaches(sender, instance, raw=False, **kwargs):
    if raw or not instance.pk:
        instance._coach_assignment_changed = False
        return
    old = sender.objects.filter(pk=instance.pk).values(
        "coach_id", "coach_2_id", "coach_3_id", "coach_count"
    ).first()
    instance._coach_assignment_changed = bool(old) and any(
        old[field] != getattr(instance, field)
        for field in ("coach_id", "coach_2_id", "coach_3_id", "coach_count")
    )


@receiver(post_save, sender=FixedLesson, dispatch_uid="club.fixed_lesson_coaches_changed", weak=False)
def fixed_lesson_coaches_changed(sender, instance, created, raw=False, **kwargs):
    if raw or created or not getattr(instance, "_coach_assignment_changed", False):
        return
    if not instance.generated_availabilities.filter(
        start_at__date__gte=timezone.localdate()
    ).exists():
        return
    synchronize_fixed_lesson_membership(instance.pk)


@receiver(
    pre_save,
    sender=Reservation,
    dispatch_uid="club.reservation_store_old_status",
    weak=False,
)
def reservation_store_old_status(sender, instance, raw=False, update_fields=None, **kwargs):
    if raw or (update_fields is not None and "status" not in update_fields):
        instance._old_status = instance.status
        return
    if not instance.pk:
        instance._old_status = None
        return

    try:
        old_status = sender.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    except Exception:
        old_status = None

    instance._old_status = old_status


@receiver(
    post_save,
    sender=Reservation,
    dispatch_uid="club.reservation_status_notification",
    weak=False,
)
def reservation_status_notification(sender, instance, created, raw=False, update_fields=None, **kwargs):
    """
    LINE無料枠を守るため、通常キャンセルは会員宛メールのみ送信します。
    雨天中止LINE通知とキャンセル待ち空き通知LINEは views.py 側で明示的に送信します。
    """
    if raw or (update_fields is not None and "status" not in update_fields):
        return
    try:
        old_status = getattr(instance, "_old_status", None)
        new_status = getattr(instance, "status", None)

        if created:
            return

        if old_status == new_status:
            return

        if new_status != Reservation.STATUS_CANCELED:
            return

        schedule_reservation_canceled_notification(instance.pk)
    except Exception as e:
        logger.warning("reservation_status_notification failed: %s", e)


@receiver(
    m2m_changed,
    sender=FixedLesson.members.through,
    dispatch_uid="club.fixed_lesson_members_changed",
    weak=False,
)
def fixed_lesson_members_changed(sender, instance, action, reverse, pk_set, **kwargs):
    """固定メンバー設定を正本として、どの更新経路でも将来予約を同期する。"""
    if kwargs.get("raw"):
        return
    if membership_signal_is_suppressed():
        return

    if action == "pre_clear" and reverse:
        instance._fixed_lesson_ids_before_clear = list(
            instance.fixed_lessons.values_list("pk", flat=True)
        )
        return

    if action not in {"post_add", "post_remove", "post_clear"}:
        return

    if reverse:
        fixed_lesson_ids = set(
            pk_set
            or getattr(instance, "_fixed_lesson_ids_before_clear", [])
        )
        if action == "post_clear" and hasattr(instance, "_fixed_lesson_ids_before_clear"):
            del instance._fixed_lesson_ids_before_clear
    else:
        fixed_lesson_ids = {instance.pk}

    for fixed_lesson_id in sorted(fixed_lesson_ids):
        synchronize_fixed_lesson_membership(fixed_lesson_id)
