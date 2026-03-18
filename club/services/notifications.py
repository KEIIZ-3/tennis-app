from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def _safe_send_mail(subject: str, body: str, recipients: list[str]):
    recipients = [r for r in recipients if r]
    if not recipients:
        return

    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@example.com"),
            recipient_list=recipients,
            fail_silently=False,
        )
    except Exception:
        logger.exception("Failed to send email notification")


def send_reservation_created_notifications(reservation):
    recipients = []
    if getattr(reservation.customer, "email", ""):
        recipients.append(reservation.customer.email)
    if reservation.coach_id and getattr(reservation.coach, "email", ""):
        recipients.append(reservation.coach.email)

    subject = f"【Tennis Club】予約作成: {reservation.date} {reservation.start_time}-{reservation.end_time}"
    body = (
        f"予約が作成されました。\n\n"
        f"種別: {reservation.get_kind_display()}\n"
        f"日付: {reservation.date}\n"
        f"時間: {reservation.start_time} - {reservation.end_time}\n"
        f"コート: {reservation.court}\n"
        f"会員: {reservation.customer.username}\n"
        f"コーチ: {reservation.coach.username if reservation.coach_id else '-'}\n"
        f"チケット使用: {reservation.tickets_used}\n"
        f"メモ: {reservation.note or '-'}\n"
    )
    _safe_send_mail(subject, body, recipients)


def send_reservation_cancelled_notifications(reservation):
    recipients = []
    if getattr(reservation.customer, "email", ""):
        recipients.append(reservation.customer.email)
    if reservation.coach_id and getattr(reservation.coach, "email", ""):
        recipients.append(reservation.coach.email)

    subject = f"【Tennis Club】予約キャンセル: {reservation.date} {reservation.start_time}-{reservation.end_time}"
    body = (
        f"予約がキャンセルされました。\n\n"
        f"種別: {reservation.get_kind_display()}\n"
        f"日付: {reservation.date}\n"
        f"時間: {reservation.start_time} - {reservation.end_time}\n"
        f"コート: {reservation.court}\n"
        f"会員: {reservation.customer.username}\n"
        f"コーチ: {reservation.coach.username if reservation.coach_id else '-'}\n"
        f"チケット返却対象: {reservation.tickets_used}\n"
        f"メモ: {reservation.note or '-'}\n"
    )
    _safe_send_mail(subject, body, recipients)
