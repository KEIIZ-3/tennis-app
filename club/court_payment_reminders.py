from datetime import datetime, time

from django.db import IntegrityError, transaction
from django.utils import timezone

from .court_fee_service import calculate_availability_court_fee
from .court_transfer_service import get_current_court_transfer_for_availability
from .lesson_execution import non_occupying_availability_ids
from .models import CoachAvailability, CourtPaymentReminderLog
from .notifications import notify_user_line_only
from .settlement_balance_policy import main_coaches


WEEKDAYS = "月火水木金土日"


def unpaid_availabilities(target_date):
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(target_date, time.min), tz)
    end = timezone.make_aware(datetime.combine(target_date, time.max), tz)
    candidates = list(
        CoachAvailability.objects.filter(
            start_at__range=(start, end),
            court_payment_status=CoachAvailability.COURT_PAYMENT_UNPAID,
        )
        .select_related("court")
        .prefetch_related("court_booking_infos__booking_account_coach")
        .order_by("start_at", "id")
    )
    canceled_ids = set(non_occupying_availability_ids(candidates))
    result = []
    for availability in candidates:
        if availability.pk in canceled_ids:
            continue
        if get_current_court_transfer_for_availability(
            availability.pk, for_update=False
        ) is not None:
            continue
        quote = calculate_availability_court_fee(availability)
        amount = (
            availability.court_fee_amount
            if availability.court_fee_overridden
            else (quote["total"] if quote else availability.court_fee_amount)
        )
        if not amount:
            continue
        result.append(availability)
    return result


def _booking_values(availability, attr):
    values = []
    for info in availability.court_booking_infos.all():
        value = getattr(info, attr)() if callable(getattr(info, attr)) else getattr(info, attr)
        value = str(value or "").strip()
        if value and value not in values:
            values.append(value)
    return " / ".join(values) or "未設定"


def build_message(target_date, availabilities):
    lines = [
        "【Play Design Tennis】",
        "コート代未払いリマインド",
        "",
        f"8日後の{target_date.month}/{target_date.day}（{WEEKDAYS[target_date.weekday()]}）に、",
        "コート代が未払いのレッスンがあります。",
        "",
    ]
    for availability in availabilities:
        start = timezone.localtime(availability.start_at)
        end = timezone.localtime(availability.end_at)
        lines.extend([
            f"・{start:%H:%M}〜{end:%H:%M}",
            f"  {availability.court_display()}",
            f"  予約番号：{_booking_values(availability, 'reservation_number')}",
            f"  予約者：{_booking_values(availability, 'booking_account_display')}",
            "",
        ])
    lines.extend([
        "コート代のお支払い後、",
        "レッスンカレンダーから支払元を登録してください。",
    ])
    return "\n".join(lines)


def send_reminders(target_date, *, dry_run=False):
    availabilities = unpaid_availabilities(target_date)
    recipients = main_coaches()
    message = build_message(target_date, availabilities) if availabilities else ""
    results = []
    if dry_run or not availabilities:
        return availabilities, recipients, message, results

    for recipient in recipients:
        try:
            with transaction.atomic():
                log, created = CourtPaymentReminderLog.objects.get_or_create(
                    target_date=target_date,
                    recipient=recipient,
                )
                if not created:
                    results.append((recipient, "already_sent"))
                    continue
                outcome = notify_user_line_only(recipient, message)
                if not outcome.get("line"):
                    log.delete()
                    results.append((recipient, "failed"))
                    continue
                results.append((recipient, "sent"))
        except IntegrityError:
            results.append((recipient, "already_sent"))
    return availabilities, recipients, message, results
