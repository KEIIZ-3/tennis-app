from dataclasses import dataclass
import calendar

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import TicketLedger, TicketPurchase, TicketPurchaseReservation, User, purchase_tickets


@dataclass(frozen=True)
class TicketProduct:
    code: str
    purchase_type: str
    ticket_count: int
    total_amount: int

    @property
    def unit_price(self):
        return self.total_amount // self.ticket_count


TICKET_PRODUCTS = (
    TicketProduct("single", TicketPurchase.PURCHASE_TYPE_SINGLE, 1, 4000),
    TicketProduct("set4", TicketPurchase.PURCHASE_TYPE_SET4, 4, 14000),
)
TICKET_PRODUCTS_BY_CODE = {product.code: product for product in TICKET_PRODUCTS}
TICKET_VALIDITY_MONTHS = 3


def ticket_expiration_from(approved_at):
    month_index = approved_at.month - 1 + TICKET_VALIDITY_MONTHS
    year = approved_at.year + month_index // 12
    month = month_index % 12 + 1
    day = min(approved_at.day, calendar.monthrange(year, month)[1])
    return approved_at.replace(year=year, month=month, day=day)


def is_main_coach(user):
    return bool(user and user.is_authenticated and user.role == User.ROLE_COACH)


def create_purchase_reservation(*, user, product_code):
    product = TICKET_PRODUCTS_BY_CODE.get((product_code or "").strip())
    if product is None:
        raise ValidationError("購入するチケットを正しく選択してください。")
    return TicketPurchaseReservation.objects.create(
        user=user, purchase_type=product.purchase_type, ticket_count=product.ticket_count,
        unit_price=product.unit_price, total_amount=product.total_amount,
    )


@transaction.atomic
def cancel_purchase_reservation(*, reservation_id, user):
    reservation = TicketPurchaseReservation.objects.select_for_update().get(pk=reservation_id, user=user)
    if reservation.status != TicketPurchaseReservation.STATUS_PENDING:
        raise ValidationError("承認待ちの購入予約だけキャンセルできます。")
    reservation.status = TicketPurchaseReservation.STATUS_CANCELED
    reservation.canceled_at = timezone.now()
    reservation.save(update_fields=["status", "canceled_at"])
    return reservation


@transaction.atomic
def approve_purchase_reservation(*, reservation_id, coach):
    if not is_main_coach(coach):
        raise PermissionDenied("メインコーチだけがチケット購入を承認できます。")
    reservation = TicketPurchaseReservation.objects.select_for_update().select_related("user", "ticket_purchase").get(pk=reservation_id)
    if reservation.status == TicketPurchaseReservation.STATUS_APPROVED and reservation.ticket_purchase_id:
        return reservation, False
    if reservation.status != TicketPurchaseReservation.STATUS_PENDING:
        raise ValidationError("承認待ちではない購入予約は承認できません。")
    approved_at = timezone.now()
    _ledger, purchase = purchase_tickets(
        user=reservation.user, tickets=reservation.ticket_count, unit_price=reservation.unit_price,
        purchase_type=reservation.purchase_type,
        reason=TicketLedger.REASON_PURCHASE_SINGLE if reservation.purchase_type == TicketPurchase.PURCHASE_TYPE_SINGLE else TicketLedger.REASON_PURCHASE_SET4,
        note=f"現金受領確認（購入予約 #{reservation.pk}）", created_by=coach,
        purchased_at=approved_at, expires_at=ticket_expiration_from(approved_at),
        idempotency_key=f"ticket-purchase-reservation:{reservation.pk}",
    )
    reservation.status = TicketPurchaseReservation.STATUS_APPROVED
    reservation.approved_at = approved_at
    reservation.approved_by = coach
    reservation.ticket_purchase = purchase
    reservation.save(update_fields=["status", "approved_at", "approved_by", "ticket_purchase"])
    return reservation, True
