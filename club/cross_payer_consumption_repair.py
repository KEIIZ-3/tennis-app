"""Repair formally recorded cross-payer consumptions with missing lot evidence."""

from dataclasses import asdict, dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from .deferred_ticket_consumption import TICKET_CONSUMPTION_FIFO_ORDER
from .models import Reservation, TicketBurdenChange, TicketConsumption, TicketPurchase, User
from .participant_price_snapshot import ticket_revenue_from_consumptions
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
    displaced_consumption_ids: tuple[int, ...] = ()
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


def _locked_displaced_consumptions(consumption_ids):
    # Reservations participating in the repair are locked separately, in
    # primary-key order. Keep this lock query on TicketConsumption itself:
    # reservation is nullable, so joining it would make PostgreSQL reject
    # FOR UPDATE on the nullable side of the outer join.
    return (
        TicketConsumption.objects.select_for_update()
        .filter(pk__in=consumption_ids)
        .order_by("id")
    )


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
        user_id=payer_id, reversed_at__isnull=True
    ).order_by("purchased_at", "id") if payer_id else TicketPurchase.objects.none()
    if lock:
        purchases = purchases.select_for_update()
    purchase = purchases.filter(remaining_tickets__gt=0).first()
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
        displaced_consumption_ids=(),
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
    if purchase and remaining >= tickets and price > 0:
        return CrossPayerRepairPreview(
            **base, candidate=True, status="candidate", reason="formal_cross_payer_evidence_confirmed"
        )
    if purchase and remaining >= tickets:
        return CrossPayerRepairPreview(**base, reason="fifo_purchase_price_missing")

    # A historical pending row may have been discovered only after a later row
    # consumed the lot. Reassign whole rows only when the canonical deferred
    # FIFO order proves an exact, unambiguous exchange.
    fifo = TicketConsumption.objects.select_related("reservation").filter(
        user_id=payer_id,
        refunded_at__isnull=True,
        reservation__status=Reservation.STATUS_ACTIVE,
        reservation__ticket_refunded_at__isnull=True,
    ).order_by(*TICKET_CONSUMPTION_FIFO_ORDER)
    if lock:
        fifo = fifo.select_for_update(of=("self",))
    fifo_rows = list(fifo)
    target_index = next((i for i, row in enumerate(fifo_rows) if row.id == consumption.id), None)
    if target_index is None:
        return CrossPayerRepairPreview(**base, reason="fifo_evidence_ambiguous")
    if any(row.purchase_id is None for row in fifo_rows[:target_index]):
        return CrossPayerRepairPreview(**base, reason="fifo_evidence_ambiguous")

    for candidate_purchase in purchases:
        if int(candidate_purchase.unit_price or 0) <= 0:
            continue
        later_linked = []
        ambiguous = False
        for row in fifo_rows[target_index + 1:]:
            if row.purchase_id is None:
                ambiguous = True
                break
            if row.purchase_id != candidate_purchase.id:
                ambiguous = True
                break
            if row.unit_price_snapshot != candidate_purchase.unit_price or _month_is_closed(row.reservation):
                ambiguous = True
                break
            active_rows = list(row.reservation.ticket_consumptions.filter(refunded_at__isnull=True))
            if (
                len(active_rows) != 1
                or ticket_revenue_from_consumptions(active_rows)
                != row.reservation.participant_ticket_price_snapshot
            ):
                ambiguous = True
                break
            later_linked.append(row)
        needed = tickets - int(candidate_purchase.remaining_tickets or 0)
        displaced = []
        released = 0
        for row in reversed(later_linked):
            displaced.append(row)
            released += int(row.tickets_used or 0)
            if released >= needed:
                break
        if ambiguous or released != needed or not displaced:
            continue
        return CrossPayerRepairPreview(
            **{
                **base,
                "purchase_id": candidate_purchase.id,
                "unit_price": int(candidate_purchase.unit_price),
                "purchase_remaining_before": int(candidate_purchase.remaining_tickets or 0),
                "purchase_remaining_after_expected": int(candidate_purchase.remaining_tickets or 0),
                "participant_ticket_price_after_expected": int(candidate_purchase.unit_price) * tickets,
                "displaced_consumption_ids": tuple(row.id for row in displaced),
                "candidate": True,
                "status": "candidate",
                "reason": "formal_cross_payer_fifo_reallocation_confirmed",
            }
        )
    return CrossPayerRepairPreview(**base, reason="fifo_purchase_capacity_insufficient")


@transaction.atomic
def repair_cross_payer_consumption(reservation_id):
    reservation = Reservation.objects.get(pk=reservation_id)
    payer_ids = {reservation.user_id}
    payer_ids.update(
        TicketConsumption.objects.filter(reservation_id=reservation_id).values_list("user_id", flat=True)
    )
    list(User.objects.select_for_update().filter(pk__in=sorted(payer_ids)).order_by("pk"))
    list(
        TicketPurchase.objects.select_for_update()
        .filter(user_id__in=sorted(payer_ids))
        .order_by("user_id", "purchased_at", "id")
    )
    related_reservation_ids = set(
        TicketConsumption.objects.filter(
            user_id__in=sorted(payer_ids), refunded_at__isnull=True
        ).exclude(reservation_id=None).values_list("reservation_id", flat=True)
    )
    related_reservation_ids.add(reservation_id)
    list(
        Reservation.objects.select_for_update()
        .filter(pk__in=sorted(related_reservation_ids)).order_by("pk")
    )
    list(
        TicketConsumption.objects.select_for_update()
        .filter(user_id__in=sorted(payer_ids)).order_by("id")
    )
    reservation = Reservation.objects.get(pk=reservation_id)
    preview = inspect_cross_payer_consumption_repair(reservation_id, lock=True)
    if preview.status == "noop":
        return preview
    if not preview.candidate:
        raise CrossPayerRepairRejected(preview.reason)

    purchase = TicketPurchase.objects.select_for_update().get(pk=preview.purchase_id)
    consumption = TicketConsumption.objects.select_for_update().get(pk=preview.consumption_id)
    displaced = list(_locked_displaced_consumptions(preview.displaced_consumption_ids))
    if displaced:
        if sum(int(row.tickets_used or 0) for row in displaced) != int(consumption.tickets_used or 0):
            raise CrossPayerRepairRejected("fifo_evidence_changed")
        for row in displaced:
            row.purchase = None
            row.unit_price_snapshot = None
            row.save(update_fields=["purchase", "unit_price_snapshot"])
            Reservation.objects.filter(pk=row.reservation_id).update(
                participant_ticket_price_snapshot=None
            )
    else:
        if purchase.remaining_tickets < consumption.tickets_used:
            raise CrossPayerRepairRejected("fifo_purchase_capacity_insufficient")
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
