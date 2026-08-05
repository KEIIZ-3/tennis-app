import logging

from django.db.models.signals import m2m_changed, post_save, pre_save
from django.dispatch import receiver

from .fixed_lesson_sync_facade import synchronize_fixed_lesson_membership
from .models import FixedLesson, Reservation
from .notifications import build_reservation_canceled_message, notify_user_email_only

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=Reservation)
def reservation_store_old_status(sender, instance, **kwargs):
    if not instance.pk:
        instance._old_status = None
        return

    try:
        old_status = sender.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    except Exception:
        old_status = None

    instance._old_status = old_status


@receiver(post_save, sender=Reservation)
def reservation_status_notification(sender, instance, created, **kwargs):
    """
    LINE無料枠を守るため、通常キャンセルは会員宛メールのみ送信します。
    雨天中止LINE通知とキャンセル待ち空き通知LINEは views.py 側で明示的に送信します。
    """
    try:
        old_status = getattr(instance, "_old_status", None)
        new_status = getattr(instance, "status", None)

        if created:
            return

        if old_status == new_status:
            return

        if new_status != Reservation.STATUS_CANCELED:
            return

        message = build_reservation_canceled_message(instance)

        notify_user_email_only(
            instance.user,
            message,
            subject="【Play Design Tennis】予約キャンセル通知",
        )
    except Exception as e:
        logger.warning("reservation_status_notification failed: %s", e)


@receiver(m2m_changed, sender=FixedLesson.members.through)
def fixed_lesson_members_changed(sender, instance, action, reverse, pk_set, **kwargs):
    """固定メンバー設定を正本として、どの更新経路でも将来予約を同期する。"""
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
