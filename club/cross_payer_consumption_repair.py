"""Repair formally recorded cross-payer consumptions with missing lot evidence."""

from dataclasses import asdict, dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Reservation, TicketBurdenChange, TicketConsumption, TicketPurchase, User
from .settlement_models import MonthlySettlement


class CrossPayerRepairRejected(ValidationError):
    pass


@dataclass(frozen=True)
class CrossPayerRepairPreview:
    reservation_id: int
    consumption_id: int | None
    payer_id: int | None
    burden_change_id: int | None
    purchase_id: int | None
    unit_price: int | None
    purchase_remaining_before: int | None
    purchase_remaining_after_expected: int | None
    participant_ticket_price_before: int | None
    participant_ticket_price_after_expected: int | None
    candidate: bool = False
    status: str = "rejected"
    reason: str = ""

    def to_dict(self):
        return asdict(self)


def _month_is_closed(reservation):
    return MonthlySettlement.objects.filter(
        year=reservation.start_at.year,
        month=reservation.start_at.month,
        status=MonthlySettlement.STATUS_CLOSED,
    ).exists()


def inspect_cross_payer_consumption_repair(reservation_id, *, lock=False):
    reservations = Reservation.objects.select_for_update() if lock else Reservation.objects
    reservation = reservations.get(pk=reservation_id)
    active = TicketConsumption.objects.filter(
        reservation_id=reservation_id, refunded_at__isnull=True
    ).order_by("id")
    if lock:
        active = active.select_for_update()
    consumptions = list(active)
    changes = TicketBurdenChange.objects
    if lock:
        changes = changes.select_for_update()
    latest_change = (
        changes.filter(reservation_id=reservation_id)
        .order_by("-created_at", "-id")
        .first()
    )
    consumption = consumptions[0] if len(consumptions) == 1 else None
    payer_id = consumption.user_id if consumption else None
    purchases = TicketPurchase.objects.filter(
        user_id=payer_id, remaining_tickets__gt=0, reversed_at__isnull=True
    ).order_by("purchased_at", "id") if payer_id else TicketPurchase.objects.none()
    if lock:
        purchases = purchases.select_for_update()
    purchase = purchases.first()
    tickets = int(consumption.tickets_used or 0) if consumption else 0
    price = int(purchase.unit_price) if purchase else None
    remaining = int(purchase.remaining_tickets) if purchase else None
    base = dict(
        reservation_id=reservation.id,
        consumption_id=consumption.id if consumption else None,
        payer_id=payer_id,
        burden_change_id=latest_change.id if latest_change else None,
        purchase_id=purchase.id if purchase else None,
        unit_price=price,
        purchase_remaining_before=remaining,
        purchase_remaining_after_expected=None if remaining is None else remaining - tickets,
        participant_ticket_price_before=reservation.participant_ticket_price_snapshot,
        participant_ticket_price_after_expected=None if price is None else price * tickets,
    )
    if reservation.status != Reservation.STATUS_ACTIVE:
        return CrossPayerRepairPreview(**base, reason="reservation_not_active")
    if _month_is_closed(reservation):
        return CrossPayerRepairPreview(**base, reason="accounting_month_closed")
    if len(consumptions) != 1:
        return CrossPayerRepairPreview(**base, reason="single_active_consumption_required")
    if consumption.purchase_id is not None or consumption.unit_price_snapshot is not None:
        return CrossPayerRepairPreview(**base, status="noop", reason="consumption_already_priced")
    if not latest_change:
        return CrossPayerRepairPreview(**base, reason="formal_burden_change_required")
    if (
        latest_change.previous_payer_id != reservation.user_id
        or latest_change.new_payer_id != consumption.user_id
        or int(latest_change.tickets) != tickets
        or tickets != int(reservation.tickets_used or 0)
    ):
        return CrossPayerRepairPreview(**base, reason="latest_burden_change_mismatch")
    if not purchase:
        return CrossPayerRepairPreview(**base, reason="fifo_purchase_capacity_insufficient")
    if remaining < tickets:
        return CrossPayerRepairPreview(**base, reason="fifo_purchase_capacity_insufficient")
    if price <= 0:
        return CrossPayerRepairPreview(**base, reason="fifo_purchase_price_missing")
    return CrossPayerRepairPreview(
        **base, candidate=True, status="candidate", reason="formal_cross_payer_evidence_confirmed"
    )


@transaction.atomic
def repair_cross_payer_consumption(reservation_id):
    reservation = Reservation.objects.select_for_update().get(pk=reservation_id)
    payer_ids = {reservation.user_id}
    payer_ids.update(
        TicketConsumption.objects.filter(reservation_id=reservation_id).values_list("user_id", flat=True)
    )
    list(User.objects.select_for_update().filter(pk__in=sorted(payer_ids)).order_by("pk"))
    list(
        TicketConsumption.objects.select_for_update()
        .filter(reservation_id=reservation_id).order_by("id")
    )
    preview = inspect_cross_payer_consumption_repair(reservation_id, lock=True)
    if preview.status == "noop":
        return preview
    if not preview.candidate:
        raise CrossPayerRepairRejected(preview.reason)

    purchase = TicketPurchase.objects.select_for_update().get(pk=preview.purchase_id)
    if purchase.remaining_tickets < TicketConsumption.objects.get(pk=preview.consumption_id).tickets_used:
        raise CrossPayerRepairRejected("fifo_purchase_capacity_insufficient")
    consumption = TicketConsumption.objects.select_for_update().get(pk=preview.consumption_id)
    purchase.remaining_tickets -= consumption.tickets_used
    purchase.save(update_fields=["remaining_tickets"])
    consumption.purchase = purchase
    consumption.unit_price_snapshot = purchase.unit_price
    consumption.save(update_fields=["purchase", "unit_price_snapshot"])
    reservation.participant_ticket_price_snapshot = purchase.unit_price * consumption.tickets_used
    Reservation.objects.filter(pk=reservation.pk).update(
        participant_ticket_price_snapshot=reservation.participant_ticket_price_snapshot
    )
    return CrossPayerRepairPreview(
        **{**preview.to_dict(), "status": "repaired", "reason": "cross_payer_evidence_restored"}
    )
