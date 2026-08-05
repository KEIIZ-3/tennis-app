"""Compatibility facade for the former duplicate notification implementation."""

from club.notification_service import deliver_to_users
from club.notifications import send_email_to_address, send_line_to_id, verify_line_signature


def send_email_notification(subject: str, message: str, recipient_list):
    recipients = list(dict.fromkeys(
        str(email or "").strip().lower() for email in recipient_list if str(email or "").strip()
    ))
    return bool(recipients) and all(
        send_email_to_address(email, subject, message) for email in recipients
    )


def send_line_push(line_user_id, text):
    return send_line_to_id(line_user_id, text)


def notify_user(user, subject, message):
    aggregate = deliver_to_users(
        [user], subject=subject, message=message, media=("line", "email")
    )
    return {
        "line": bool(aggregate["line_sent"]),
        "email": bool(aggregate["email_sent"]),
    }


def build_reservation_created_message(reservation):
    subject = "【テニスクラブ】予約完了"
    message = (
        "予約が完了しました。\n"
        f"日時: {reservation.start_at:%Y-%m-%d %H:%M} - {reservation.end_at:%H:%M}\n"
        f"コーチ: {reservation.coach.username}\n"
        f"コート: {reservation.court.name}\n"
    )
    return subject, message


def build_reservation_canceled_message(reservation):
    subject = "【テニスクラブ】予約キャンセル完了"
    message = (
        "予約キャンセルを受け付けました。\n"
        f"日時: {reservation.start_at:%Y-%m-%d %H:%M} - {reservation.end_at:%H:%M}\n"
        f"コーチ: {reservation.coach.username}\n"
        f"コート: {reservation.court.name}\n"
    )
    return subject, message
