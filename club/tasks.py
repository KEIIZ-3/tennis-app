from celery import shared_task
from django.conf import settings

from .notifications import send_email_to_address, send_line_to_id

@shared_task
def notify_email(subject: str, message: str, to_email: str):
    return send_email_to_address(to_email, subject, message)

@shared_task
def notify_line_notify(message: str):
    # LINE Notify is retired. Preserve the task name without retaining a second
    # external HTTP implementation.
    return False

@shared_task
def notify_line_messaging_api(message: str):
    to_user = getattr(settings, "LINE_TO_USER_ID", "")
    return send_line_to_id(to_user, message)

def build_reservation_message(action: str, reservation) -> str:
    # reservation が start/end/coach をどこに持つかは既存モデル次第なので、
    # ここは signals 側で組み立てて渡してもOK
    return f"[{action}] 予約ID={reservation.id}"
