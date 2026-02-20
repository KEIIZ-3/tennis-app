from django.conf import settings
from django.core.mail import send_mail


def _can_send():
    return bool(getattr(settings, "DEFAULT_FROM_EMAIL", "")) and bool(getattr(settings, "EMAIL_HOST", ""))


def send_reservation_created(reservation):
    if not _can_send():
        return

    subject = "【Tennis Club】予約が確定しました"
    coach = getattr(reservation.coach, "username", "-") if reservation.coach_id else "-"
    court = getattr(reservation.court, "name", "-")
    body = (
        f"予約が確定しました。\n\n"
        f"種別: {reservation.kind}\n"
        f"日付: {reservation.date}\n"
        f"時間: {reservation.start_time} - {reservation.end_time}\n"
        f"コーチ: {coach}\n"
        f"コート: {court}\n"
        f"チケット消費: {reservation.tickets_used}\n"
    )
    to_list = [reservation.customer.email] if reservation.customer.email else []
    if to_list:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, to_list, fail_silently=True)


def send_reservation_cancelled(reservation):
    if not _can_send():
        return

    subject = "【Tennis Club】予約がキャンセルされました"
    coach = getattr(reservation.coach, "username", "-") if reservation.coach_id else "-"
    court = getattr(reservation.court, "name", "-")
    body = (
        f"予約がキャンセルされました。\n\n"
        f"種別: {reservation.kind}\n"
        f"日付: {reservation.date}\n"
        f"時間: {reservation.start_time} - {reservation.end_time}\n"
        f"コーチ: {coach}\n"
        f"コート: {court}\n"
        f"返却チケット: {reservation.tickets_used}\n"
    )
    to_list = [reservation.customer.email] if reservation.customer.email else []
    if to_list:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, to_list, fail_silently=True)
