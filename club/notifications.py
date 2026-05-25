import base64
import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

logger = logging.getLogger(__name__)


def _safe_display_name(user):
    if not user:
        return "ユーザー"
    try:
        return user.display_name()
    except Exception:
        return getattr(user, "username", "ユーザー")


def _format_datetime_range(start_at, end_at):
    if not start_at or not end_at:
        return "日時未定"

    try:
        start_local = timezone.localtime(start_at) if timezone.is_aware(start_at) else start_at
        end_local = timezone.localtime(end_at) if timezone.is_aware(end_at) else end_at
        return f"{start_local:%Y-%m-%d %H:%M}〜{end_local:%H:%M}"
    except Exception:
        return f"{start_at}〜{end_at}"


def _lesson_type_label(obj):
    if not obj:
        return "-"
    try:
        return obj.get_lesson_type_display()
    except Exception:
        return getattr(obj, "lesson_type", "-") or "-"


def _court_label(obj):
    court = getattr(obj, "court", None)
    if court:
        return str(court)
    return "-"


def _payment_label(obj):
    if not obj:
        return "-"
    try:
        return obj.payment_label()
    except Exception:
        tickets = int(getattr(obj, "tickets_used", 0) or 0)
        if tickets <= 0:
            return "チケット使用なし"
        if tickets == 1:
            return "チケット1枚"
        return f"チケット{tickets}枚"


def _reservation_common_lines(reservation):
    assigned_coach = None
    try:
        assigned_coach = reservation.assigned_coach()
    except Exception:
        assigned_coach = getattr(reservation, "substitute_coach", None) or getattr(reservation, "coach", None)

    return [
        f"会員: {_safe_display_name(getattr(reservation, 'user', None))}",
        f"コーチ: {_safe_display_name(assigned_coach)}",
        f"種別: {_lesson_type_label(reservation)}",
        f"日時: {_format_datetime_range(getattr(reservation, 'start_at', None), getattr(reservation, 'end_at', None))}",
        f"コート: {_court_label(reservation)}",
        f"お支払い・チケット: {_payment_label(reservation)}",
    ]


def build_pending_request_for_coach_message(reservation):
    lines = [
        "【Play Design Tennis】新しいレッスン申請があります。",
        "",
        *_reservation_common_lines(reservation),
        "",
        "コーチ画面から承認または却下をお願いします。",
    ]

    requested_court_note = (getattr(reservation, "requested_court_note", "") or "").strip()
    if requested_court_note:
        lines.insert(-2, f"希望・備考: {requested_court_note}")

    return "\n".join(lines)


def build_request_approved_for_member_message(reservation):
    lines = [
        "【Play Design Tennis】レッスン申請が承認されました。",
        "",
        *_reservation_common_lines(reservation),
        "",
        "予約一覧から内容をご確認ください。",
    ]

    approved_court_note = (getattr(reservation, "approved_court_note", "") or "").strip()
    if approved_court_note:
        lines.insert(-2, f"コート連絡: {approved_court_note}")

    return "\n".join(lines)


def build_request_rejected_for_member_message(reservation):
    reason = (getattr(reservation, "cancellation_reason", "") or "コーチ却下").strip()
    return "\n".join(
        [
            "【Play Design Tennis】レッスン申請が却下されました。",
            "",
            *_reservation_common_lines(reservation),
            f"理由: {reason}",
            "",
            "必要に応じて、別日時で再申請をお願いします。",
        ]
    )


def build_reservation_rain_canceled_message(reservation):
    reason = (getattr(reservation, "cancellation_reason", "") or "雨天中止").strip()
    return "\n".join(
        [
            "【Play Design Tennis】レッスンが雨天中止になりました。",
            "",
            *_reservation_common_lines(reservation),
            f"理由: {reason}",
            "",
            "使用済みチケットがある場合は返却処理されています。",
        ]
    )


def build_reservation_created_message(reservation):
    return "\n".join(
        [
            "【Play Design Tennis】予約が完了しました。",
            "",
            *_reservation_common_lines(reservation),
            "",
            "予約一覧から内容をご確認ください。",
        ]
    )


def build_waitlist_registered_for_member_email_message(waitlist):
    assigned_coach = None
    try:
        assigned_coach = waitlist.assigned_coach()
    except Exception:
        assigned_coach = getattr(waitlist, "substitute_coach", None) or getattr(waitlist, "coach", None)

    lines = [
        "【Play Design Tennis】キャンセル待ちに登録しました。",
        "",
        f"会員: {_safe_display_name(getattr(waitlist, 'user', None))}",
        f"コーチ: {_safe_display_name(assigned_coach)}",
        f"種別: {_lesson_type_label(waitlist)}",
        f"日時: {_format_datetime_range(getattr(waitlist, 'start_at', None), getattr(waitlist, 'end_at', None))}",
        f"コート: {_court_label(waitlist)}",
        "",
        "空きが出た場合は、LINEでご案内します。",
        "この時点では予約は確定していません。",
    ]
    return "\n".join(lines)


def build_reservation_canceled_message(reservation):
    reason = (getattr(reservation, "cancellation_reason", "") or "キャンセル").strip()
    return "\n".join(
        [
            "【Play Design Tennis】予約がキャンセルされました。",
            "",
            *_reservation_common_lines(reservation),
            f"理由: {reason}",
        ]
    )


def build_stringing_order_created_for_coach_message(order):
    user = getattr(order, "user", None)
    delivery_requested = bool(getattr(order, "delivery_requested", False))
    delivery_label = "あり" if delivery_requested else "なし"

    lines = [
        "【Play Design Tennis】新しいガット張り依頼があります。",
        "",
        f"会員: {_safe_display_name(user)}",
        f"ラケット: {(getattr(order, 'racket_name', '') or '-').strip() or '-'}",
        f"ガット: {(getattr(order, 'string_name', '') or '-').strip() or '-'}",
        f"テンション: {getattr(order, 'tension_lbs', '-') } lbs",
        f"デリバリー: {delivery_label}",
        f"希望日時/納期: {(getattr(order, 'preferred_delivery_time', '') or '-').strip() or '-'}",
        f"料金: {order.total_price()}円" if hasattr(order, "total_price") else "料金: -",
    ]

    delivery_location = (getattr(order, "delivery_location", "") or "").strip()
    if delivery_requested and delivery_location:
        lines.insert(-2, f"届け場所: {delivery_location}")

    note = (getattr(order, "note", "") or "").strip()
    if note:
        lines.append(f"備考: {note}")

    return "\n".join(lines)


def verify_line_signature(body, signature):
    channel_secret = (getattr(settings, "LINE_CHANNEL_SECRET", "") or "").strip()
    if not channel_secret:
        logger.warning("LINE_CHANNEL_SECRET is not configured.")
        return False

    if not signature:
        return False

    digest = hmac.new(
        channel_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected_signature = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected_signature, signature)


def _line_push_message(line_user_id, message_text):
    channel_access_token = (getattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "") or "").strip()
    if not channel_access_token:
        logger.info("LINE_CHANNEL_ACCESS_TOKEN is not configured. Skip LINE push.")
        return False

    if not line_user_id:
        return False

    payload = {
        "to": line_user_id,
        "messages": [
            {
                "type": "text",
                "text": str(message_text)[:5000],
            }
        ],
    }

    request = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {channel_access_token}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
        return True
    except urllib.error.HTTPError as e:
        try:
            error_body = e.read().decode("utf-8")
        except Exception:
            error_body = str(e)
        logger.warning("LINE push failed: %s", error_body)
        return False
    except Exception as e:
        logger.warning("LINE push failed: %s", e)
        return False


def notify_line_messaging_api(user, message_text):
    if not user or not message_text:
        return False

    try:
        line_link = getattr(user, "line_link", None)
    except Exception:
        line_link = None

    if not line_link:
        return False

    if not getattr(line_link, "is_active", False):
        return False

    line_user_id = (getattr(line_link, "line_user_id", "") or "").strip()
    return _line_push_message(line_user_id, message_text)


def notify_email(user, subject, message_text):
    if not user or not message_text:
        return False

    email = (getattr(user, "email", "") or "").strip()
    if not email:
        return False

    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "") or None

    try:
        send_mail(
            subject=subject,
            message=message_text,
            from_email=from_email,
            recipient_list=[email],
            fail_silently=False,
        )
        return True
    except Exception as e:
        logger.warning("Email notification failed for user=%s: %s", getattr(user, "pk", None), e)
        return False


def notify_admins(subject, message_text):
    recipients = []

    admin_notify_email = (getattr(settings, "ADMIN_NOTIFY_EMAIL", "") or "").strip()
    if admin_notify_email:
        recipients.extend(
            [email.strip() for email in admin_notify_email.split(",") if email.strip()]
        )

    for _name, email in getattr(settings, "ADMINS", []):
        if email:
            recipients.append(email)

    recipients = list(dict.fromkeys(recipients))
    if not recipients:
        return False

    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "") or None

    try:
        send_mail(
            subject=subject,
            message=message_text,
            from_email=from_email,
            recipient_list=recipients,
            fail_silently=False,
        )
        return True
    except Exception as e:
        logger.warning("Admin email notification failed: %s", e)
        return False


def notify_user_line_only(user, message_text, subject="Play Design Tennis 通知"):
    """LINEだけ送信します。月200通制限があるため、雨天中止・キャンセル待ち空き通知など即時性が高い用途に限定します。"""
    line_ok = False
    try:
        line_ok = notify_line_messaging_api(user, message_text)
    except Exception as e:
        logger.warning("notify_user_line_only failed: %s", e)

    return {
        "line": line_ok,
        "email": False,
    }


def notify_user_email_only(user, message_text, subject="Play Design Tennis 通知"):
    """メールだけ送信します。通常予約・キャンセル・キャンセル待ち登録・個別レッスン申請系に使います。"""
    email_ok = False
    try:
        email_ok = notify_email(user, subject, message_text)
    except Exception as e:
        logger.warning("notify_user_email_only failed: %s", e)

    return {
        "line": False,
        "email": email_ok,
    }


def notify_user_both(user, message_text, subject="Play Design Tennis 通知"):
    """必要時のみLINEとメールの両方を送信する互換用関数です。通常運用では原則使いません。"""
    line_result = notify_user_line_only(user, message_text, subject=subject)
    email_result = notify_user_email_only(user, message_text, subject=subject)
    return {
        "line": bool(line_result.get("line")),
        "email": bool(email_result.get("email")),
    }


def notify_user(user, message_text, subject="Play Design Tennis 通知"):
    """
    互換用。
    LINE無料枠を守るため、既存コードが notify_user を呼んだ場合もメールのみ送信します。
    LINEを送る場合は notify_user_line_only を明示的に使います。
    """
    return notify_user_email_only(user, message_text, subject=subject)


# 旧実装名が残っていても落ちないようにする互換関数
def notify_line_notify(message_text):
    logger.info("LINE Notify is deprecated / not used. message=%s", message_text)
    return False
