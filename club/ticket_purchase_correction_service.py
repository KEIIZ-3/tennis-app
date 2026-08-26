"""Transactional correction of an immutable ticket purchase history row."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .deferred_ticket_consumption import TICKET_CONSUMPTION_FIFO_ORDER
from .models import (
    Reservation,
    TicketCashReceipt,
    TicketConsumption,
    TicketLedger,
    TicketPurchase,
    TicketPurchaseReservation,
    User,
    apply_ticket_change,
    ensure_accounting_month_is_open,
    purchase_tickets,
)
from .participant_price_snapshot import ticket_revenue_from_consumptions
from .ticket_cash_receipt_service import record_ticket_cash_receipt, reverse_ticket_cash_receipt


def _is_admin(user):
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))
    )


def _purchase_reason(purchase_type):
    if purchase_type == TicketPurchase.PURCHASE_TYPE_SINGLE:
        return TicketLedger.REASON_PURCHASE_SINGLE
    if purchase_type == TicketPurchase.PURCHASE_TYPE_SET4:
        return TicketLedger.REASON_PURCHASE_SET4
    return TicketLedger.REASON_ADMIN_ADJUST


def _active_receipt(purchase):
    return purchase.cash_receipts.filter(reversed_at__isnull=True).order_by("id").first()


def correction_impact(purchase, *, tickets, unit_price, cash_amount=None):
    used = TicketConsumption.objects.filter(
        purchase=purchase, refunded_at__isnull=True
    ).values_list("tickets_used", flat=True)
    receipt = _active_receipt(purchase)
    return {
        "balance_change": int(tickets) - int(purchase.total_tickets),
        "used_tickets": sum(int(value) for value in used),
        "receipt": receipt,
        "cash_difference": (
            int(cash_amount) - int(receipt.amount)
            if receipt is not None and cash_amount is not None
            else None
        ),
        "new_total": int(tickets) * int(unit_price),
    }


def _lock_and_validate_open_months(purchase, receipt, demands, purchased_at, received_at):
    ensure_accounting_month_is_open(purchase.purchased_at)
    ensure_accounting_month_is_open(purchased_at)
    if receipt is not None:
        ensure_accounting_month_is_open(receipt.received_at)
        ensure_accounting_month_is_open(received_at)
    for demand in demands:
        if demand.reservation_id:
            ensure_accounting_month_is_open(demand.reservation.start_at)


def _reallocate_active_consumptions(*, user, demands, corrected_at, reason):
    """Rebuild active consumption evidence in canonical FIFO order; keep old rows."""
    purchases = list(
        TicketPurchase.objects.select_for_update()
        .filter(user=user, reversed_at__isnull=True)
        .order_by("purchased_at", "id")
    )
    remaining = {purchase.pk: int(purchase.total_tickets) for purchase in purchases}
    affected_reservation_ids = {row.reservation_id for row in demands if row.reservation_id}

    for row in demands:
        row.refunded_at = corrected_at
        row.refund_note = f"付与履歴修正によるFIFO再配賦: {reason}"
        row.save(update_fields=["refunded_at", "refund_note"])

    ordered_demands = sorted(
        demands,
        key=lambda row: (
            row.reservation.ticket_consumed_at if row.reservation_id else row.created_at,
            row.reservation_id or 0,
            row.pk,
        ),
    )
    for demand in ordered_demands:
        needed = int(demand.tickets_used)
        for purchase in purchases:
            if needed <= 0:
                break
            available = remaining[purchase.pk]
            if available <= 0:
                continue
            allocated = min(available, needed)
            created = TicketConsumption.objects.create(
                user=user,
                purchase=purchase,
                reservation_id=demand.reservation_id,
                fixed_lesson_id=demand.fixed_lesson_id,
                tickets_used=allocated,
                unit_price_snapshot=purchase.unit_price,
            )
            remaining[purchase.pk] -= allocated
            needed -= allocated
        if needed:
            created = TicketConsumption.objects.create(
                user=user,
                purchase=None,
                reservation_id=demand.reservation_id,
                fixed_lesson_id=demand.fixed_lesson_id,
                tickets_used=needed,
                unit_price_snapshot=None,
            )

    for purchase in purchases:
        purchase.remaining_tickets = remaining[purchase.pk]
        purchase.save(update_fields=["remaining_tickets"])

    for reservation in Reservation.objects.select_for_update().filter(pk__in=affected_reservation_ids):
        rows = list(
            reservation.ticket_consumptions.filter(refunded_at__isnull=True).order_by("created_at", "id")
        )
        reservation.participant_ticket_price_snapshot = ticket_revenue_from_consumptions(rows)
        reservation.save(update_fields=["participant_ticket_price_snapshot"])


@transaction.atomic
def correct_ticket_purchase(
    *, purchase_id, actor, tickets, unit_price, purchase_type, purchased_at, note,
    reason, idempotency_key, cash_mode="none", cash_amount=None, cash_received_at=None,
):
    if not _is_admin(actor):
        raise PermissionDenied("チケット付与履歴を修正できるのは管理者だけです。")
    if int(tickets) <= 0 or int(unit_price) < 0:
        raise ValidationError("枚数は1以上、単価は0以上にしてください。")
    if purchase_type not in dict(TicketPurchase.PURCHASE_TYPE_CHOICES):
        raise ValidationError("購入種別を正しく選択してください。")
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("修正理由は必須です。")
    token = (idempotency_key or "").strip()
    if not token:
        raise ValidationError("二重送信防止キーがありません。")

    purchase = TicketPurchase.objects.select_for_update().select_related("user").get(pk=purchase_id)
    try:
        return purchase.corrected_to, False
    except TicketPurchase.DoesNotExist:
        pass
    if purchase.reversed_at:
        raise ValidationError("取消済みまたは修正済みの付与履歴は修正できません。")

    locked_user = User.objects.select_for_update().get(pk=purchase.user_id)
    demands = list(
        TicketConsumption.objects.select_for_update()
        .select_related("reservation")
        .filter(
            user=locked_user,
            refunded_at__isnull=True,
            reservation__status=Reservation.STATUS_ACTIVE,
            reservation__ticket_refunded_at__isnull=True,
        )
        .order_by(*TICKET_CONSUMPTION_FIFO_ORDER)
    )
    active_receipts = list(TicketCashReceipt.objects.select_for_update().filter(
        ticket_purchase=purchase, reversed_at__isnull=True
    ).order_by("id"))
    if len(active_receipts) > 1:
        raise ValidationError("有効な現金受領記録が複数あるため、安全に修正できません。")
    receipt = active_receipts[0] if active_receipts else None
    if receipt is None and cash_mode != "none":
        raise ValidationError("現金受領記録がないため、現金受領の変更はできません。")
    if receipt is not None:
        if cash_mode not in ("preserve", "replace"):
            raise ValidationError("現金受領記録の扱いを明示してください。")
        if cash_mode == "preserve":
            cash_amount, cash_received_at = receipt.amount, receipt.received_at
        elif cash_amount is None or cash_received_at is None:
            raise ValidationError("修正後の現金受領額と受領日を入力してください。")

    _lock_and_validate_open_months(
        purchase, receipt, demands, purchased_at, cash_received_at
    )
    corrected_at = timezone.now()

    if receipt is not None:
        reverse_ticket_cash_receipt(
            receipt_id=receipt.pk,
            reversed_by=actor,
            reason=f"付与履歴修正: {reason}",
            reversed_at=corrected_at,
        )

    for active_purchase in TicketPurchase.objects.select_for_update().filter(
        user=locked_user, reversed_at__isnull=True
    ):
        active_purchase.remaining_tickets = active_purchase.total_tickets
        active_purchase.save(update_fields=["remaining_tickets"])

    for demand in demands:
        demand.refunded_at = corrected_at
        demand.refund_note = f"付与履歴修正によるFIFO再配賦: {reason}"
        demand.save(update_fields=["refunded_at", "refund_note"])

    apply_ticket_change(
        user=locked_user,
        amount=-purchase.total_tickets,
        reason=TicketLedger.REASON_PURCHASE_REVERSAL,
        note=f"付与履歴 #{purchase.pk} 修正取消: {reason}",
        created_by=actor,
    )
    purchase.remaining_tickets = 0
    purchase.reversed_at = corrected_at
    purchase.reversed_by = actor
    purchase.reversal_reason = "correction"
    purchase.save(update_fields=["remaining_tickets", "reversed_at", "reversed_by", "reversal_reason"])
    TicketPurchaseReservation.objects.select_for_update().filter(
        ticket_purchase=purchase,
        status=TicketPurchaseReservation.STATUS_APPROVED,
    ).update(
        status=TicketPurchaseReservation.STATUS_REVERSED,
        reversed_at=corrected_at,
        reversed_by=actor,
        reversal_reason="correction",
    )

    _ledger, replacement = purchase_tickets(
        user=locked_user,
        tickets=int(tickets),
        unit_price=int(unit_price),
        purchase_type=purchase_type,
        reason=_purchase_reason(purchase_type),
        note=note,
        created_by=actor,
        purchased_at=purchased_at,
        label=purchase.label,
        expires_at=purchase.expires_at,
        idempotency_key=f"ticket-correction:{purchase.pk}:{token}",
    )
    replacement.corrected_from = purchase
    replacement.correction_reason = reason
    replacement.save(update_fields=["corrected_from", "correction_reason"])

    if receipt is not None:
        record_ticket_cash_receipt(
            ticket_purchase=replacement,
            amount=int(cash_amount),
            received_at=cash_received_at,
            created_by=actor,
            payment_method=receipt.payment_method,
            idempotency_key=f"ticket-correction:{purchase.pk}:{token}:cash",
        )

    # The archived rows are the immutable demand evidence for the replacement FIFO run.
    archived = list(
        TicketConsumption.objects.filter(pk__in=[row.pk for row in demands]).select_related("reservation")
    )
    _reallocate_active_consumptions(
        user=locked_user, demands=archived, corrected_at=corrected_at, reason=reason
    )
    locked_user.refresh_from_db(fields=["ticket_balance"])
    replacement.refresh_from_db()
    return replacement, True
