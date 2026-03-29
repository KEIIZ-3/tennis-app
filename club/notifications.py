import base64
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request

from django.apps import apps
from django.utils import timezone


LINE_PUSH_API_URL = "https://api.line.me/v2/bot/message/push"
LINE_REPLY_API_URL = "https://api.line.me/v2/bot/message/reply"


def _get_env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _get_channel_access_token() -> str:
    return _get_env("LINE_CHANNEL_ACCESS_TOKEN")


def _get_channel_secret() -> str:
    return _get_env("LINE_CHANNEL_SECRET")


def _line_request(url: str, payload: dict) -> tuple[bool, str]:
    token = _get_channel_access_token()
    if not token:
        return False, "LINE_CHANNEL_ACCESS_TOKEN is not set."

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            body = res.read().decode("utf-8", errors="ignore")
            return True, body
    except urllib.error.HTTPError as e:
        try:
            error_body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            error_body = str(e)
        return False, error_body
    except Exception as e:
        return False, str(e)


def send_line_push(line_user_id: str, message: str) -> tuple[bool, str]:
    if not line_user_id:
        return False, "line_user_id is empty."

    payload = {
        "to": line_user_id,
        "messages": [
            {
                "type": "text",
                "text": message,
            }
        ],
    }
    return _line_request(LINE_PUSH_API_URL, payload)


def send_line_reply(reply_token: str, message: str) -> tuple[bool, str]:
    if not reply_token:
        return False, "reply_token is empty."

    payload = {
        "replyToken": reply_token,
        "messages": [
            {
                "type": "text",
                "text": message,
            }
        ],
    }
    return _line_request(LINE_REPLY_API_URL, payload)


def verify_line_signature(body: bytes, signature: str) -> bool:
    secret = _get_channel_secret()
    if not secret:
        return False

    if not isinstance(body, (bytes, bytearray)):
        body = (body or "").encode("utf-8")

    digest = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected_signature = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected_signature, signature or "")


def _get_line_account_link_model():
    try:
        return apps.get_model("club", "LineAccountLink")
    except Exception:
        return None


def _get_user_display(user) -> str:
    if not user:
        return "ユーザー"

    if hasattr(user, "get_full_name"):
        try:
            value = user.get_full_name()
            if value:
                return str(value)
        except Exception:
            pass

    for attr in ("first_name", "full_name", "name", "username", "email"):
        value = getattr(user, attr, None)
        if value:
            return str(value)

    return "ユーザー"


def notify_user(user, message: str) -> tuple[bool, str]:
    if not user or not message:
        return False, "user or message is empty."

    LineAccountLink = _get_line_account_link_model()
    if LineAccountLink is None:
        return False, "LineAccountLink model not found."

    try:
        link = LineAccountLink.objects.filter(user=user, is_active=True).first()
    except Exception as e:
        return False, f"failed to lookup LineAccountLink: {e}"

    if not link:
        return False, "active LineAccountLink not found."

    line_user_id = getattr(link, "line_user_id", "") or ""
    if not line_user_id:
        return False, "line_user_id is empty."

    return send_line_push(line_user_id=line_user_id, message=message)


def _fmt_dt(value) -> str:
    if not value:
        return "未設定"
    try:
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)


def _pick_first_attr(obj, names, default=""):
    for name in names:
        value = getattr(obj, name, None)
        if value not in (None, ""):
            return value
    return default


def _extract_reservation_data(reservation):
    user = _pick_first_attr(reservation, ["user", "customer", "member", "owner"], None)
    coach = _pick_first_attr(reservation, ["coach"], None)
    court = _pick_first_attr(reservation, ["court"], None)
    start_at = _pick_first_attr(reservation, ["start_at", "start", "starts_at"], None)
    end_at = _pick_first_attr(reservation, ["end_at", "end", "ends_at"], None)
    availability = _pick_first_attr(reservation, ["coach_availability", "availability"], None)

    if not start_at and availability is not None:
        start_at = _pick_first_attr(availability, ["start_at", "start", "starts_at"], None)
    if not end_at and availability is not None:
        end_at = _pick_first_attr(availability, ["end_at", "end", "ends_at"], None)
    if coach is None and availability is not None:
        coach = _pick_first_attr(availability, ["coach"], None)
    if court is None and availability is not None:
        court = _pick_first_attr(availability, ["court"], None)

    return {
        "user_name": _get_user_display(user),
        "coach_name": str(coach) if coach else "未設定",
        "court_name": str(court) if court else "未設定",
        "start_text": _fmt_dt(start_at),
        "end_text": _fmt_dt(end_at),
    }


def build_reservation_created_message(reservation) -> str:
    data = _extract_reservation_data(reservation)
    return (
        "【予約完了】\n"
        "ご予約ありがとうございます。\n\n"
        f"利用者: {data['user_name']}\n"
        f"コーチ: {data['coach_name']}\n"
        f"コート: {data['court_name']}\n"
        f"開始: {data['start_text']}\n"
        f"終了: {data['end_text']}\n\n"
        "内容をご確認ください。"
    )


def build_reservation_canceled_message(reservation) -> str:
    data = _extract_reservation_data(reservation)
    return (
        "【予約キャンセル】\n"
        "ご予約のキャンセルを受け付けました。\n\n"
        f"利用者: {data['user_name']}\n"
        f"コーチ: {data['coach_name']}\n"
        f"コート: {data['court_name']}\n"
        f"開始: {data['start_text']}\n"
        f"終了: {data['end_text']}\n\n"
        "必要に応じて、あらためてご予約ください。"
    )
