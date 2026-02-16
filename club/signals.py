from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver

from .models import Reservation
from .tasks import notify_email, notify_line_notify, notify_line_messaging_api

def _msg(action: str, r: Reservation) -> str:
    return (
        f"[{action}] "
        f"{r.date} {r.start_time}-{r.end_time} / court={r.court} / "
        f"coach={getattr(r.coach, 'username', '-') } / customer={getattr(r.customer, 'username', '-')}"
    )

@receiver(pre_save, sender=Reservation)
def reservation_pre_save(sender, instance: Reservation, **kwargs):
    # 変更前statusを覚える（重複通知対策）
    if instance.pk:
        try:
            old = Reservation.objects.only("status").get(pk=instance.pk)
            instance._old_status = old.status
        except Exception:
            instance._old_status = None
    else:
        instance._old_status = None

@receiver(post_save, sender=Reservation)
def reservation_post_save(sender, instance: Reservation, created: bool, **kwargs):
    # created=新規 booked、または booked→cancelled に変わった時だけ通知
    old_status = getattr(instance, "_old_status", None)

    if created and instance.status == "booked":
        action = "予約作成"
    elif old_status == "booked" and instance.status == "cancelled":
        action = "予約キャンセル"
    else:
        return

    message = _msg(action, instance)

    # ---- 同期送信（Worker不要）----
    try:
        if instance.customer and instance.customer.email:
            notify_email(subject="テニスクラブ通知", message=message, to_email=instance.customer.email)
    except Exception:
        pass

    try:
        notify_line_notify(message=message)
    except Exception:
        pass

    try:
        notify_line_messaging_api(message=message)
    except Exception:
        pass
