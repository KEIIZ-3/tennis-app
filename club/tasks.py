from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.utils.timezone import localtime
import urllib.request
import urllib.parse
import json

@shared_task
def notify_email(subject: str, message: str, to_email: str):
    if not to_email:
        return
    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[to_email],
        fail_silently=True,
    )

@shared_task
def notify_line_notify(message: str):
    token = getattr(settings, "LINE_NOTIFY_TOKEN", "")
    if not token:
        return
    data = urllib.parse.urlencode({"message": message}).encode("utf-8")
    req = urllib.request.Request(
        "https://notify-api.line.me/api/notify",
        data=data,
        headers={"Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass

@shared_task
def notify_line_messaging_api(message: str):
    token = getattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "")
    to_user = getattr(settings, "LINE_TO_USER_ID", "")
    if not token or not to_user:
        return

    payload = {"to": to_user, "messages": [{"type": "text", "text": message}]}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push",
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass

def build_reservation_message(action: str, reservation) -> str:
    # reservation が start/end/coach をどこに持つかは既存モデル次第なので、
    # ここは signals 側で組み立てて渡してもOK
    return f"[{action}] 予約ID={reservation.id}"
