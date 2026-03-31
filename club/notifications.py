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

    try:
        if hasattr(user, "display_name"):
            value = user.display_name()
            if value:
                return str(value)
    except Exception:
        pass

    for attr in ("full_name", "first_name", "name", "username", "email"):
        value = getattr(user, attr, None)
        if value:
            return str(value)

    if hasattr(user, "get_full_name"):
        try:
            value = user.get_full_name()
            if value:
                return str(value)
        except Exception:
            pass

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


def notify_users(users, message: str):
    results = []
    seen_ids = set()

    for user in users:
        if not user or not getattr(user, "pk", None):
            continue
        if user.pk in seen_ids:
            continue
        seen_ids.add(user.pk)
        results.append((user, *notify_user(user, message)))
    return results


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
    substitute_coach = _pick_first_attr(reservation, ["substitute_coach"], None)
    court = _pick_first_attr(reservation, ["court"], None)
    start_at = _pick_first_attr(reservation, ["start_at", "start", "starts_at"], None)
    end_at = _pick_first_attr(reservation, ["end_at", "end", "ends_at"], None)
    availability = _pick_first_attr(reservation, ["coach_availability", "availability"], None)
    lesson_type = _pick_first_attr(reservation, ["lesson_type"], "")
    tickets_used = _pick_first_attr(reservation, ["tickets_used"], 0)
    requested_court_note = _pick_first_attr(reservation, ["requested_court_note"], "")
    approved_court_note = _pick_first_attr(reservation, ["approved_court_note"], "")
    cancellation_reason = _pick_first_attr(reservation, ["cancellation_reason"], "")

    if not start_at and availability is not None:
        start_at = _pick_first_attr(availability, ["start_at", "start", "starts_at"], None)
    if not end_at and availability is not None:
        end_at = _pick_first_attr(availability, ["end_at", "end", "ends_at"], None)
    if coach is None and availability is not None:
        coach = _pick_first_attr(availability, ["coach"], None)
    if court is None and availability is not None:
        court = _pick_first_attr(availability, ["court"], None)

    lesson_type_text_map = {
        "general": "一般レッスン",
        "private": "プライベートレッスン",
        "group": "グループレッスン",
        "event": "イベント",
    }
    lesson_type_text = lesson_type_text_map.get(lesson_type, "レッスン")

    assigned_coach = substitute_coach or coach
    balance_text = ""
    if user is not None and hasattr(user, "ticket_balance"):
        balance_text = f"{user.ticket_balance}"

    return {
        "user_name": _get_user_display(user),
        "coach_name": _get_user_display(coach) if coach else "未設定",
        "substitute_coach_name": _get_user_display(substitute_coach) if substitute_coach else "",
        "assigned_coach_name": _get_user_display(assigned_coach) if assigned_coach else "未設定",
        "court_name": str(court) if court else "未設定",
        "start_text": _fmt_dt(start_at),
        "end_text": _fmt_dt(end_at),
        "lesson_type_text": lesson_type_text,
        "tickets_used": tickets_used,
        "ticket_balance": balance_text,
        "requested_court_note": str(requested_court_note or "").strip(),
        "approved_court_note": str(approved_court_note or "").strip(),
        "cancellation_reason": str(cancellation_reason or "").strip(),
    }


def build_reservation_created_message(reservation) -> str:
    data = _extract_reservation_data(reservation)
    return (
        "【予約完了】\n"
        "ご予約ありがとうございます。\n\n"
        f"お名前: {data['user_name']}\n"
        f"レッスン種別: {data['lesson_type_text']}\n"
        f"コーチ: {data['assigned_coach_name']}\n"
        f"コート: {data['court_name']}\n"
        f"開始: {data['start_text']}\n"
        f"終了: {data['end_text']}\n"
        f"消費チケット: {data['tickets_used']}枚\n"
        f"現在残数: {data['ticket_balance']}枚\n\n"
        "内容をご確認ください。"
    )


def build_reservation_canceled_message(reservation) -> str:
    data = _extract_reservation_data(reservation)
    return (
        "【予約キャンセル】\n"
        "ご予約のキャンセルを受け付けました。\n\n"
        f"お名前: {data['user_name']}\n"
        f"レッスン種別: {data['lesson_type_text']}\n"
        f"コーチ: {data['assigned_coach_name']}\n"
        f"コート: {data['court_name']}\n"
        f"開始: {data['start_text']}\n"
        f"終了: {data['end_text']}\n"
        f"返却チケット: {data['tickets_used']}枚\n"
        f"現在残数: {data['ticket_balance']}枚\n\n"
        "必要に応じて、あらためてご予約ください。"
    )


def build_reservation_rain_canceled_message(reservation) -> str:
    data = _extract_reservation_data(reservation)
    return (
        "【雨天中止】\n"
        "本日のレッスンは雨天のため中止となりました。\n\n"
        f"お名前: {data['user_name']}\n"
        f"レッスン種別: {data['lesson_type_text']}\n"
        f"コーチ: {data['assigned_coach_name']}\n"
        f"コート: {data['court_name']}\n"
        f"開始: {data['start_text']}\n"
        f"終了: {data['end_text']}\n"
        f"返却チケット: {data['tickets_used']}枚\n"
        f"現在残数: {data['ticket_balance']}枚\n\n"
        "またのご予約をお待ちしております。"
    )


def build_pending_request_for_coach_message(reservation) -> str:
    data = _extract_reservation_data(reservation)
    message = (
        "【新規申請】\n"
        "プライベート / グループの申請が入りました。\n\n"
        f"会員: {data['user_name']}\n"
        f"種別: {data['lesson_type_text']}\n"
        f"担当コーチ: {data['coach_name']}\n"
        f"実施コーチ: {data['assigned_coach_name']}\n"
        f"コート: {data['court_name']}\n"
        f"開始: {data['start_text']}\n"
        f"終了: {data['end_text']}\n"
    )
    if data["requested_court_note"]:
        message += f"申請メモ: {data['requested_court_note']}\n"
    message += "\n管理画面から承認・却下をお願いします。"
    return message


def build_request_approved_for_member_message(reservation) -> str:
    data = _extract_reservation_data(reservation)
    message = (
        "【申請承認】\n"
        "ご申請いただいた予約が承認されました。\n\n"
        f"種別: {data['lesson_type_text']}\n"
        f"担当コーチ: {data['assigned_coach_name']}\n"
        f"コート: {data['court_name']}\n"
        f"開始: {data['start_text']}\n"
        f"終了: {data['end_text']}\n"
        f"消費チケット: {data['tickets_used']}枚\n"
    )
    if data["ticket_balance"]:
        message += f"現在残数: {data['ticket_balance']}枚\n"
    if data["approved_court_note"]:
        message += f"承認メモ: {data['approved_court_note']}\n"
    message += "\n当日のご参加をお待ちしております。"
    return message


def build_request_rejected_for_member_message(reservation) -> str:
    data = _extract_reservation_data(reservation)
    message = (
        "【申請却下】\n"
        "ご申請いただいた予約は今回は承認されませんでした。\n\n"
        f"種別: {data['lesson_type_text']}\n"
        f"担当コーチ: {data['assigned_coach_name']}\n"
        f"コート: {data['court_name']}\n"
        f"開始: {data['start_text']}\n"
        f"終了: {data['end_text']}\n"
    )
    if data["cancellation_reason"]:
        message += f"理由: {data['cancellation_reason']}\n"
    message += "\n別枠でのご予約もご検討ください。"
    return message
