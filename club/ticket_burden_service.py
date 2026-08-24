from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .lesson_participants import reservations_for_object
from .models import (
    Reservation,
    TicketBurdenChange,
    TicketConsumption,
    TicketLedger,
    TicketPurchase,
    User,
    apply_ticket_change,
    ensure_accounting_month_is_open,
)


def _consume_for_payer(*, reservation, payer, tickets, created_by, purchases):
    purchases = [row for row in purchases if row.remaining_tickets > 0]
    evidenced = sum(int(row.remaining_tickets or 0) for row in purchases)
    unknown = max(int(payer.ticket_balance or 0) - evidenced, 0)
    remaining = int(tickets)
    unknown_used = min(unknown, remaining)
    remaining -= unknown_used
    for purchase in purchases:
        used = min(int(purchase.remaining_tickets), remaining)
        if used <= 0:
            continue
        purchase.remaining_tickets -= used
        purchase.save(update_fields=["remaining_tickets"])
        TicketConsumption.objects.create(
            user=payer,
            purchase=purchase,
            reservation=reservation,
            fixed_lesson=reservation.fixed_lesson,
            tickets_used=used,
            unit_price_snapshot=purchase.unit_price,
        )
        remaining -= used
        if remaining == 0:
            break
    pending = unknown_used + remaining
    if pending:
        TicketConsumption.objects.create(
            user=payer,
            purchase=None,
            reservation=reservation,
            fixed_lesson=reservation.fixed_lesson,
            tickets_used=pending,
            unit_price_snapshot=None,
        )
    apply_ticket_change(
        user=payer,
        amount=-tickets,
        reason=TicketLedger.REASON_RESERVATION_USE,
        note=f"チケット負担変更: 予約 #{reservation.pk}",
        created_by=created_by,
        reservation=reservation,
        fixed_lesson=reservation.fixed_lesson,
    )


@transaction.atomic
def change_lesson_ticket_burden(*, reservation_payers, created_by):
    """Set one actual payer for each active reservation in a lesson occurrence."""
    if not reservation_payers:
        raise ValidationError("負担内容を指定してください。")
    requested_ids = sorted(int(pk) for pk in reservation_payers)
    reservations = list(
        Reservation.objects.select_for_update()
        .select_related("availability", "fixed_lesson")
        .filter(pk__in=requested_ids)
        .order_by("pk")
    )
    if len(reservations) != len(requested_ids):
        raise ValidationError("対象予約が見つかりません。")
    canonical_ids = list(
        reservations_for_object(reservations[0])
        .filter(tickets_used__gt=0)
        .values_list("pk", flat=True)
    )
    if requested_ids != sorted(canonical_ids):
        raise ValidationError("同じレッスンの有効な予約をすべて指定してください。")
    ensure_accounting_month_is_open(reservations[0].start_at)
    if any(row.status != Reservation.STATUS_ACTIVE for row in reservations):
        raise ValidationError("有効な予約のみ負担変更できます。")
    if any(not row.user_id for row in reservations):
        raise ValidationError("ゲストのチケット負担は変更できません。")

    active_by_reservation = {
        reservation.pk: list(
            reservation.ticket_consumptions.select_for_update()
            .filter(refunded_at__isnull=True)
            .order_by("id")
        )
        for reservation in reservations
    }
    payer_ids = sorted(
        {int(value) for value in reservation_payers.values()}
        | {
            consumption.user_id
            for rows in active_by_reservation.values()
            for consumption in rows
        }
    )
    payers = {
        row.pk: row
        for row in User.objects.select_for_update().filter(pk__in=payer_ids).order_by("pk")
    }
    if len(payers) != len(payer_ids):
        raise ValidationError("負担者が見つかりません。")
    all_locked_purchases = list(
        TicketPurchase.objects.select_for_update()
        .filter(user_id__in=payer_ids)
        .order_by("user_id", "purchased_at", "id")
    )
    locked_purchases = {
        row.pk: row
        for row in all_locked_purchases
    }
    purchases_by_user = {
        payer_id: [row for row in all_locked_purchases if row.user_id == payer_id]
        for payer_id in payer_ids
    }

    changes = []
    now = timezone.now()
    for reservation in reservations:
        desired = payers[int(reservation_payers[reservation.pk])]
        active = active_by_reservation[reservation.pk]
        current_ids = {row.user_id for row in active}
        current_total = sum(int(row.tickets_used or 0) for row in active)
        if current_ids == {desired.pk} and current_total == reservation.tickets_used:
            continue
        if len(current_ids) != 1 or current_total != reservation.tickets_used:
            raise ValidationError(f"予約 #{reservation.pk} の消費証跡が負担変更可能な状態ではありません。")
        previous_id = next(iter(current_ids))
        for consumption in active:
            if consumption.purchase_id:
                purchase = locked_purchases[consumption.purchase_id]
                purchase.remaining_tickets += consumption.tickets_used
                if purchase.remaining_tickets > purchase.total_tickets:
                    purchase.remaining_tickets = purchase.total_tickets
                purchase.save(update_fields=["remaining_tickets"])
            consumption.refunded_at = now
            consumption.refund_note = "チケット負担変更による付替返却"
            consumption.save(update_fields=["refunded_at", "refund_note"])
        apply_ticket_change(
            user=payers[previous_id],
            amount=reservation.tickets_used,
            reason=TicketLedger.REASON_CANCEL_REFUND,
            note=f"チケット負担変更返却: 予約 #{reservation.pk}",
            created_by=created_by,
            reservation=reservation,
            fixed_lesson=reservation.fixed_lesson,
        )
        _consume_for_payer(
            reservation=reservation,
            payer=desired,
            tickets=reservation.tickets_used,
            created_by=created_by,
            purchases=purchases_by_user[desired.pk],
        )
        changes.append(TicketBurdenChange.objects.create(
            reservation=reservation,
            previous_payer_id=previous_id,
            new_payer=desired,
            tickets=reservation.tickets_used,
            created_by=created_by,
        ))
    return changes
