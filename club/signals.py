import logging

from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver

from .models import Reservation
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
