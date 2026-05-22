import logging

from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver

from .models import Reservation
from .notifications import build_reservation_canceled_message, notify_admins, notify_user

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
    既存の views.py 側で、申請・承認・却下・雨天中止・ガット張り通知は直接送っています。
    ここでは二重通知を避けるため、主に「通常キャンセル」の保険通知だけ扱います。
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

        notify_user(
            instance.user,
            message,
            subject="【Play Design Tennis】予約キャンセル通知",
        )

        assigned_coach = instance.substitute_coach or instance.coach
        notify_user(
            assigned_coach,
            message,
            subject="【Play Design Tennis】予約キャンセル通知",
        )

        notify_admins(
            "【Play Design Tennis】予約キャンセル通知",
            message,
        )
    except Exception as e:
        logger.warning("reservation_status_notification failed: %s", e)
