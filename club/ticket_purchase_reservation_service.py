from dataclasses import dataclass
import calendar

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction
from django.utils import timezone

from .models import TicketConsumption, TicketLedger, TicketPurchase, TicketPurchaseReservation, User, apply_ticket_change, ensure_accounting_month_is_open, purchase_tickets


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
REVERSAL_REASON_CHOICES = (
    ("test", "テスト登録"),
    ("mistake", "誤承認"),
    ("correction", "購入内容訂正"),
    ("other", "その他"),
)
REVERSAL_REASON_VALUES = {value for value, _label in REVERSAL_REASON_CHOICES}


def _locked_purchase_reservations():
    """Lock reservation rows without joining nullable related tables.

    PostgreSQL rejects ``FOR UPDATE`` when a nullable ``select_related`` join
    puts the related table on the nullable side of an outer join.  Related
    records needed by the workflow are therefore fetched separately.
    """
    return TicketPurchaseReservation.objects.select_for_update()


def ticket_expiration_from(approved_at):
    month_index = approved_at.month - 1 + TICKET_VALIDITY_MONTHS
    year = approved_at.year + month_index // 12
    month = month_index % 12 + 1
    day = min(approved_at.day, calendar.monthrange(year, month)[1])
    return approved_at.replace(year=year, month=month, day=day)


def is_main_coach(user):
    return bool(user and user.is_authenticated and user.role == User.ROLE_COACH)


def pending_purchase_reservations_for_participants(reservations):
    """Return each pending purchase reservation owned by an active participant.

    The caller supplies the canonical Reservation queryset for one occurrence.
    Keeping this boundary explicit prevents recurring membership or waitlists from
    being mistaken for attendance, while preserving multiple purchases per user.
    """
    participant_user_ids = reservations.exclude(user_id=None).values_list(
        "user_id", flat=True
    )
    return TicketPurchaseReservation.objects.filter(
        user_id__in=participant_user_ids,
        status=TicketPurchaseReservation.STATUS_PENDING,
    ).select_related("user").order_by(
        "user__full_name", "user__username", "requested_at", "id"
    )


def completed_purchase_reservations_for_participants(reservations):
    reservation_ids = list(reservations.values_list("id", flat=True))
    participant_user_ids = reservations.exclude(user_id=None).values_list("user_id", flat=True)
    return TicketPurchaseReservation.objects.filter(
        status__in=(TicketPurchaseReservation.STATUS_APPROVED, TicketPurchaseReservation.STATUS_REVERSED),
        ticket_purchase__isnull=False,
    ).filter(
        models.Q(approved_for_reservation_id__in=reservation_ids)
        | models.Q(approved_for_reservation__isnull=True, user_id__in=participant_user_ids)
    ).select_related("user", "approved_by", "reversed_by", "ticket_purchase").order_by("-approved_at", "-id")


def purchase_reversal_availability(purchase_reservation, *, purchase=None):
    if purchase is None:
        purchase = purchase_reservation.ticket_purchase
    if purchase_reservation.status == TicketPurchaseReservation.STATUS_REVERSED:
        return False, "すでに承認取消済みです"
    if purchase_reservation.status != TicketPurchaseReservation.STATUS_APPROVED or purchase is None:
        return False, "承認済み購入ではありません"
    if purchase.reversed_at:
        return False, "すでに承認取消済みです"
    if purchase.remaining_tickets != purchase.total_tickets or TicketConsumption.objects.filter(purchase=purchase).exists():
        return False, "この購入で付与したチケットが既に使用されているため、自動取消できません"
    return True, ""


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
def approve_purchase_reservation(*, reservation_id, coach, approved_for_reservation=None):
    if not is_main_coach(coach):
        raise PermissionDenied("メインコーチだけがチケット購入を承認できます。")
    reservation = _locked_purchase_reservations().get(pk=reservation_id)
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
    reservation.approved_for_reservation = approved_for_reservation
    reservation.save(update_fields=["status", "approved_at", "approved_by", "ticket_purchase", "approved_for_reservation"])
    return reservation, True


@transaction.atomic
def reverse_purchase_reservation(*, reservation_id, coach, reason):
    if not is_main_coach(coach):
        raise PermissionDenied("メインコーチだけがチケット購入承認を取り消せます。")
    if reason not in REVERSAL_REASON_VALUES:
        raise ValidationError("取消理由を選択してください。")
    reservation = _locked_purchase_reservations().get(pk=reservation_id)
    if reservation.status == TicketPurchaseReservation.STATUS_REVERSED:
        return reservation, False
    if reservation.status != TicketPurchaseReservation.STATUS_APPROVED or not reservation.ticket_purchase_id:
        raise ValidationError("承認済みの購入だけ取消できます。")
    purchase = TicketPurchase.objects.select_for_update().get(pk=reservation.ticket_purchase_id)
    ensure_accounting_month_is_open(purchase.purchased_at)
    User.objects.select_for_update().get(pk=reservation.user_id)
    can_reverse, error = purchase_reversal_availability(reservation, purchase=purchase)
    if not can_reverse:
        raise ValidationError(error)
    reversed_at = timezone.now()
    reason_label = dict(REVERSAL_REASON_CHOICES)[reason]
    apply_ticket_change(
        user=reservation.user,
        amount=-purchase.total_tickets,
        reason=TicketLedger.REASON_PURCHASE_REVERSAL,
        note=f"購入予約 #{reservation.pk} 承認取消: {reason_label}",
        created_by=coach,
    )
    purchase.remaining_tickets = 0
    purchase.reversed_at = reversed_at
    purchase.reversed_by = coach
    purchase.reversal_reason = reason
    purchase.save(update_fields=["remaining_tickets", "reversed_at", "reversed_by", "reversal_reason"])
    reservation.status = TicketPurchaseReservation.STATUS_REVERSED
    reservation.reversed_at = reversed_at
    reservation.reversed_by = coach
    reservation.reversal_reason = reason
    reservation.save(update_fields=["status", "reversed_at", "reversed_by", "reversal_reason"])
    return reservation, True
