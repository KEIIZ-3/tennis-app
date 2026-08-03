from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Reservation, TicketLedger, TicketConsumption, User


REPAIR_NOTE_PREFIX = "固定予約チケット補正"


def _missing_consumption_queryset():
    """
    固定参加予約のうち、チケット枚数は設定済みだが実際の消費処理が完了していない
    有効予約だけを返す。

    ticket_consumed_at が未設定で、TicketConsumption も存在しない予約に限定するため、
    既に正常に消費済みの予約は対象外になる。
    """
    return (
        Reservation.objects.filter(
            is_fixed_entry=True,
            fixed_lesson_id__isnull=False,
            status__in=(Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING),
            tickets_used__gt=0,
            ticket_consumed_at__isnull=True,
            ticket_refunded_at__isnull=True,
        )
        .exclude(ticket_consumptions__isnull=False)
        .select_related("user", "fixed_lesson")
        .order_by("start_at", "id")
    )


def repair_missing_fixed_ticket_consumptions(*, created_by=None, past_only=True):
    """
    固定予約で未実施のチケット消費だけを補正する。

    - 既に消費済みの予約は処理しない
    - 二重実行しても ticket_consumed_at / TicketConsumption 判定で再消費しない
    - past_only=True の場合、開始日時を迎えた予約だけを補正する
    - 1件ずつトランザクションでロックして安全に処理する
    """
    queryset = _missing_consumption_queryset()
    if past_only:
        queryset = queryset.filter(start_at__lte=timezone.now())

    repaired = []
    skipped = []

    for reservation_id in queryset.values_list("id", flat=True):
        try:
            with transaction.atomic():
                reservation = (
                    Reservation.objects.select_for_update()
                    .select_related("user", "fixed_lesson")
                    .get(pk=reservation_id)
                )

                if reservation.ticket_consumed_at:
                    skipped.append((reservation.pk, "既に消費済み"))
                    continue
                if reservation.ticket_refunded_at:
                    skipped.append((reservation.pk, "返却済み"))
                    continue
                if reservation.status not in (
                    Reservation.STATUS_ACTIVE,
                    Reservation.STATUS_PENDING,
                ):
                    skipped.append((reservation.pk, "有効予約ではない"))
                    continue
                if int(reservation.tickets_used or 0) <= 0:
                    skipped.append((reservation.pk, "消費枚数0"))
                    continue
                if TicketConsumption.objects.filter(reservation=reservation).exists():
                    skipped.append((reservation.pk, "消費明細あり"))
                    continue
                if TicketLedger.objects.filter(
                    reservation=reservation,
                    reason__in=(
                        TicketLedger.REASON_RESERVATION_USE,
                        TicketLedger.REASON_FIXED_USE,
                    ),
                    change_amount__lt=0,
                ).exists():
                    skipped.append((reservation.pk, "消費台帳あり"))
                    continue

                reservation.consume_tickets(
                    reason=TicketLedger.REASON_FIXED_USE,
                    created_by=created_by,
                    note=(
                        f"{REPAIR_NOTE_PREFIX}: "
                        f"予約#{reservation.pk} "
                        f"{reservation.start_at:%Y-%m-%d %H:%M}"
                    ),
                )
                repaired.append(reservation.pk)
        except (ValidationError, User.DoesNotExist) as exc:
            skipped.append((reservation_id, str(exc)))

    return {
        "repaired_ids": repaired,
        "repaired_count": len(repaired),
        "skipped": skipped,
        "skipped_count": len(skipped),
    }
