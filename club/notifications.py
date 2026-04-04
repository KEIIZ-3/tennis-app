import base64
import hashlib
import hmac
import json
import logging
import os
import urllib.error
import urllib.request

from django.apps import apps
from django.utils import timezone


logger = logging.getLogger(__name__)

LINE_PUSH_API_URL = "https://api.line.me/v2/bot/message/push"
LINE_REPLY_API_URL = "https://api.line.me/v2/bot/message/reply"


def _get_env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _get_channel_access_token() -> str:
    return _get_env("LINE_CHANNEL_ACCESS_TOKEN")


def _get_channel_secret() -> str:
    return _get_env("LINE_CHANNEL_SECRET")


def _mask_line_user_id(line_user_id: str) -> str:
    value = str(line_user_id or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return value[:2] + "***"
    return value[:4] + "..." + value[-4:]


def _line_request(url: str, payload: dict) -> tuple[bool, str]:
    token = _get_channel_access_token()
    if not token:
        logger.warning("LINE notify skipped: LINE_CHANNEL_ACCESS_TOKEN is not set.")
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
            logger.info("LINE API success: url=%s response=%s", url, body)
            return True, body
    except urllib.error.HTTPError as e:
        try:
            error_body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            error_body = str(e)
        logger.warning("LINE API http error: url=%s status=%s body=%s", url, getattr(e, "code", ""), error_body)
        return False, error_body
    except Exception as e:
        logger.exception("LINE API unexpected error: url=%s error=%s", url, e)
        return False, str(e)


def send_line_push(line_user_id: str, message: str) -> tuple[bool, str]:
    if not line_user_id:
        logger.warning("LINE push skipped: line_user_id is empty.")
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
    logger.info(
        "LINE push attempt: to=%s message_preview=%s",
        _mask_line_user_id(line_user_id),
        (message or "")[:80],
    )
    return _line_request(LINE_PUSH_API_URL, payload)


def send_line_reply(reply_token: str, message: str) -> tuple[bool, str]:
    if not reply_token:
        logger.warning("LINE reply skipped: reply_token is empty.")
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
    logger.info("LINE reply attempt: message_preview=%s", (message or "")[:80])
    return _line_request(LINE_REPLY_API_URL, payload)


def verify_line_signature(body: bytes, signature: str) -> bool:
    secret = _get_channel_secret()
    if not secret:
        logger.warning("LINE signature verify failed: LINE_CHANNEL_SECRET is not set.")
        return False

    if not isinstance(body, (bytes, bytearray)):
        body = (body or "").encode("utf-8")

    digest = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected_signature = base64.b64encode(digest).decode("utf-8")
    matched = hmac.compare_digest(expected_signature, signature or "")
    if not matched:
        logger.warning("LINE signature mismatch.")
    return matched


def _get_line_account_link_model():
    try:
        return apps.get_model("club", "LineAccountLink")
    except Exception:
        logger.exception("Failed to load LineAccountLink model.")
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
        logger.warning("notify_user skipped: user or message is empty.")
        return False, "user or message is empty."

    user_label = _get_user_display(user)
    user_id = getattr(user, "pk", None)

    LineAccountLink = _get_line_account_link_model()
    if LineAccountLink is None:
        logger.warning("notify_user failed: LineAccountLink model not found. user_id=%s user=%s", user_id, user_label)
        return False, "LineAccountLink model not found."

    try:
        link = LineAccountLink.objects.filter(user=user, is_active=True).first()
    except Exception as e:
        logger.exception("notify_user failed: LineAccountLink lookup error. user_id=%s user=%s", user_id, user_label)
        return False, f"failed to lookup LineAccountLink: {e}"

    if not link:
        logger.warning(
            "notify_user skipped: active LineAccountLink not found. user_id=%s user=%s",
            user_id,
            user_label,
        )
        return False, "active LineAccountLink not found."

    line_user_id = getattr(link, "line_user_id", "") or ""
    if not line_user_id:
        logger.warning(
            "notify_user skipped: line_user_id is empty. user_id=%s user=%s",
            user_id,
            user_label,
        )
        return False, "line_user_id is empty."

    ok, result = send_line_push(line_user_id=line_user_id, message=message)
    if ok:
        logger.info(
            "notify_user success: user_id=%s user=%s to=%s",
            user_id,
            user_label,
            _mask_line_user_id(line_user_id),
        )
    else:
        logger.warning(
            "notify_user failed: user_id=%s user=%s to=%s result=%s",
            user_id,
            user_label,
            _mask_line_user_id(line_user_id),
            result,
        )
    return ok, result


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


def _extract_stringing_order_data(order):
    user = getattr(order, "user", None)
    assigned_coach = getattr(order, "assigned_coach", None)
    delivery_requested = bool(getattr(order, "delivery_requested", False))
    delivery_location = str(getattr(order, "delivery_location", "") or "").strip()
    preferred_delivery_time = str(getattr(order, "preferred_delivery_time", "") or "").strip()
    racket_name = str(getattr(order, "racket_name", "") or "").strip()
    string_name = str(getattr(order, "string_name", "") or "").strip()
    note = str(getattr(order, "note", "") or "").strip()

    try:
        total_price = int(order.total_price() or 0)
    except Exception:
        total_price = 0

    return {
        "user_name": _get_user_display(user),
        "assigned_coach_name": _get_user_display(assigned_coach) if assigned_coach else "未設定",
        "delivery_requested": delivery_requested,
        "delivery_location": delivery_location,
        "preferred_delivery_time": preferred_delivery_time,
        "racket_name": racket_name or "未入力",
        "string_name": string_name or "未入力",
        "note": note,
        "total_price": total_price,
    }


def build_stringing_order_created_for_coach_message(order) -> str:
    data = _extract_stringing_order_data(order)

    message = (
        "【ガット張り新規依頼】\n"
        "会員からガット張り依頼が入りました。\n\n"
        f"会員: {data['user_name']}\n"
        f"担当コーチ: {data['assigned_coach_name']}\n"
        f"ラケット名: {data['racket_name']}\n"
        f"ガット名: {data['string_name']}\n"
        f"料金: {data['total_price']}円\n"
    )

    if data["delivery_requested"]:
        message += "受け渡し: デリバリー希望\n"
        if data["delivery_location"]:
            message += f"届け場所: {data['delivery_location']}\n"
        if data["preferred_delivery_time"]:
            message += f"日時指定: {data['preferred_delivery_time']}\n"
    else:
        message += "受け渡し: レッスン時受け渡し / デリバリーなし\n"
        if data["preferred_delivery_time"]:
            message += f"希望張り上げ納期: {data['preferred_delivery_time']}\n"

    if data["note"]:
        message += f"備考: {data['note']}\n"

    message += "\n管理画面または一覧画面で内容をご確認ください。"
    return message
