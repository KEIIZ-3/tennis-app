import base64
import hashlib
import hmac
import json
import logging
import os
from urllib import request

from django.core.mail import send_mail

logger = logging.getLogger(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "no-reply@example.com")


def send_email_notification(subject: str, message: str, recipient_list):
    if not recipient_list:
        return False

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=DEFAULT_FROM_EMAIL,
            recipient_list=recipient_list,
            fail_silently=False,
        )
        return True
    except Exception:
        logger.exception("Email notification failed")
        return False


def send_line_push(line_user_id, text):
    if not LINE_CHANNEL_ACCESS_TOKEN or not line_user_id:
        return False

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {
        "to": line_user_id,
        "messages": [
            {
                "type": "text",
                "text": text[:5000],
            }
        ],
    }

    req = request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception:
        logger.exception("LINE push failed")
        return False


def verify_line_signature(body, signature):
    if not LINE_CHANNEL_SECRET or not signature:
        return False

    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def notify_user(user, subject, message):
    result = {"line": False, "email": False}

    try:
        link = getattr(user, "line_link", None)
        if link and getattr(link, "is_active", False):
            result["line"] = send_line_push(getattr(link, "line_user_id", ""), message)
    except Exception:
        logger.exception("LINE notify failed")

    email = getattr(user, "email", "")
    if email:
        result["email"] = send_email_notification(subject, message, [email])

    return result


def build_reservation_created_message(reservation):
    start_at = getattr(reservation, "start_at", None)
    end_at = getattr(reservation, "end_at", None)
    coach = getattr(reservation, "coach", None)
    court = getattr(reservation, "court", None)

    subject = "【テニスクラブ】予約完了"
    message = "予約が完了しました。\n"

    if start_at:
        message += f"日時: {start_at:%Y-%m-%d %H:%M}"
        if end_at:
            message += f" - {end_at:%H:%M}"
        message += "\n"

    if coach is not None:
        coach_name = getattr(coach, "username", str(coach))
        message += f"コーチ: {coach_name}\n"

    if court is not None:
        court_name = getattr(court, "name", str(court))
        message += f"コート: {court_name}\n"

    return subject, message


def build_reservation_canceled_message(reservation):
    start_at = getattr(reservation, "start_at", None)
    end_at = getattr(reservation, "end_at", None)
    coach = getattr(reservation, "coach", None)
    court = getattr(reservation, "court", None)

    subject = "【テニスクラブ】予約キャンセル完了"
    message = "予約キャンセルを受け付けました。\n"

    if start_at:
        message += f"日時: {start_at:%Y-%m-%d %H:%M}"
        if end_at:
            message += f" - {end_at:%H:%M}"
        message += "\n"

    if coach is not None:
        coach_name = getattr(coach, "username", str(coach))
        message += f"コーチ: {coach_name}\n"

    if court is not None:
        court_name = getattr(court, "name", str(court))
        message += f"コート: {court_name}\n"

    return subject, message
