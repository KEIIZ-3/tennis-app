from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from .models import Reservation
from .tasks import notify_email, notify_line_notify, notify_line_messaging_api

def _msg(action: str, r: Reservation) -> str:
    return (
        f"[{action}] "
        f"{r.date} {r.start_time}-{r.end_time} / court={r.court} / "
        f"coach={getattr(r.coach, 'username', '-') } / customer={getattr(r.customer, 'username', '-')}"
    )

@receiver(post_save, sender=Reservation)
def on_reservation_saved(sender, instance: Reservation, created: bool, **kwargs):
    # booked / cancelled のみ
    if instance.status not in ("booked", "cancelled"):
        return

    action = "予約作成" if created and instance.status == "booked" else ("予約キャンセル" if instance.status == "cancelled" else "予約更新")
    message = _msg(action, instance)

    # customer にメール
    try:
        if instance.customer and instance.customer.email:
            notify_email.delay("テニスクラブ通知", message, instance.customer.email)
    except Exception:
        pass

    # LINE（ENVが入ってる方だけ動く）
    try:
        notify_line_notify.delay(message)
    except Exception:
        pass
    try:
        notify_line_messaging_api.delay(message)
    except Exception:
        pass
