from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import TicketCashReceipt, TicketPurchase, ensure_accounting_month_is_open


@transaction.atomic
def record_ticket_cash_receipt(
    *, ticket_purchase, amount, received_at, created_by,
    payment_method=TicketCashReceipt.PAYMENT_METHOD_CASH, idempotency_key=None,
):
    if not created_by or not getattr(created_by, "pk", None):
        raise ValidationError("現金受領の処理者が必要です。")
    if int(amount) <= 0:
        raise ValidationError("現金受領額は1円以上にしてください。")
    if payment_method not in dict(TicketCashReceipt.PAYMENT_METHOD_CHOICES):
        raise ValidationError("対応していない支払方法です。")
    ensure_accounting_month_is_open(received_at)
    normalized_key = (idempotency_key or "").strip() or None
    if normalized_key:
        existing = TicketCashReceipt.objects.filter(idempotency_key=normalized_key).first()
        if existing:
            return existing, False
    try:
        with transaction.atomic():
            receipt = TicketCashReceipt.objects.create(
                ticket_purchase=ticket_purchase,
                amount=amount,
                received_at=received_at,
                payment_method=payment_method,
                created_by=created_by,
                idempotency_key=normalized_key,
            )
    except IntegrityError:
        if normalized_key:
            return TicketCashReceipt.objects.get(idempotency_key=normalized_key), False
        raise
    return receipt, True


@transaction.atomic
def reverse_ticket_cash_receipt(*, receipt_id, reversed_by, reason, reversed_at=None):
    if not reversed_by or not getattr(reversed_by, "pk", None):
        raise ValidationError("現金受領取消の処理者が必要です。")
    if not (reason or "").strip():
        raise ValidationError("現金受領取消の理由が必要です。")
    receipt = TicketCashReceipt.objects.select_for_update().get(pk=receipt_id)
    if receipt.reversed_at:
        return receipt, False
    ensure_accounting_month_is_open(receipt.received_at)
    receipt.reversed_at = reversed_at or timezone.now()
    receipt.reversed_by = reversed_by
    receipt.reversal_reason = reason.strip()
    receipt.save(update_fields=["reversed_at", "reversed_by", "reversal_reason"])
    return receipt, True
