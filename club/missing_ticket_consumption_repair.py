"""Diagnose and repair ledgered reservations with missing consumption evidence."""

from dataclasses import asdict, dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum

from .models import Reservation, TicketConsumption, TicketLedger, TicketPurchase, User
from .participant_price_snapshot import set_participant_ticket_price_snapshot
from .settlement_models import MonthlySettlement


class MissingConsumptionRepairRejected(ValidationError):
    pass


@dataclass(frozen=True)
class MissingConsumptionPreview:
    user: str
    user_id: int
    reservation_id: int
    lesson_date: str
    ledger_consumption_count: int
    ticket_consumption_exists: bool
    current_purchase_id: int | None
    expected_purchase_id: int | None
    current_unit_price: int | None
    expected_unit_price: int | None
    purchase_remaining_before: int | None
    purchase_remaining_after_expected: int | None
    participant_ticket_price_before: int | None
    participant_ticket_price_after_expected: int | None
    candidate: bool
    reason: str

    def to_dict(self):
        return asdict(self)


def _name(user):
    return user.full_name or user.username


def _closed(reservation):
    return MonthlySettlement.objects.filter(
        year=reservation.start_at.year,
        month=reservation.start_at.month,
        status=MonthlySettlement.STATUS_CLOSED,
    ).exists()


def _missing_reservations(user_id):
    return list(
        Reservation.objects.filter(
            user_id=user_id,
            status=Reservation.STATUS_ACTIVE,
            ticket_consumed_at__isnull=False,
            ticket_refunded_at__isnull=True,
            tickets_used__gt=0,
        )
        .annotate(
            use_ledger_count=Count(
                "ticket_ledgers",
                filter=Q(
                    ticket_ledgers__reason=TicketLedger.REASON_RESERVATION_USE
                ),
            ),
            use_ledger_delta=Sum(
                "ticket_ledgers__change_amount",
                filter=Q(
                    ticket_ledgers__reason=TicketLedger.REASON_RESERVATION_USE
                ),
            ),
            active_consumption_count=Count(
                "ticket_consumptions",
                filter=Q(
                    ticket_consumptions__refunded_at__isnull=True
                ),
            ),
            total_consumption_count=Count("ticket_consumptions"),
        )
        .filter(active_consumption_count=0, total_consumption_count=0)
        .order_by("ticket_consumed_at", "id")
    )


def inspect_missing_ticket_consumptions(*, user_ids=None):
    ids = user_ids or list(
        Reservation.objects.filter(
            status=Reservation.STATUS_ACTIVE,
            ticket_consumed_at__isnull=False,
            ticket_refunded_at__isnull=True,
            tickets_used__gt=0,
        ).values_list("user_id", flat=True).distinct()
    )
    rows = []
    for user in User.objects.filter(pk__in=ids).order_by("pk"):
        missing = _missing_reservations(user.pk)
        if not missing:
            continue
        earliest_missing_at = missing[0].ticket_consumed_at
        active = list(
            TicketConsumption.objects.filter(
                user=user, refunded_at__isnull=True,
                reservation__status=Reservation.STATUS_ACTIVE,
                reservation__ticket_refunded_at__isnull=True,
            ).select_related("reservation", "purchase")
        )
        active = [
            row for row in active
            if row.purchase_id is None or row.purchase.purchased_at > earliest_missing_at
        ]
        purchases = list(TicketPurchase.objects.filter(user=user).order_by("purchased_at", "id"))
        purchases = [row for row in purchases if row.purchased_at > earliest_missing_at]
        capacity = {p.pk: int(p.remaining_tickets) for p in purchases}
        for consumption in active:
            if consumption.purchase_id:
                capacity[consumption.purchase_id] += int(consumption.tickets_used)

        demands = [(r.ticket_consumed_at, r.pk, r, None) for r in missing]
        demands += [
            (c.reservation.ticket_consumed_at, c.reservation_id, c.reservation, c)
            for c in active
        ]
        demands.sort(key=lambda item: (item[0], item[1], item[3].pk if item[3] else 0))
        expected = {}
        remaining = capacity.copy()
        for consumed_at, reservation_id, reservation, consumption in demands:
            needed = int(consumption.tickets_used if consumption else reservation.tickets_used)
            allocations = []
            for purchase in purchases:
                if purchase.purchased_at <= consumed_at or remaining[purchase.pk] <= 0:
                    continue
                used = min(needed, remaining[purchase.pk])
                allocations.append((purchase, used))
                remaining[purchase.pk] -= used
                needed -= used
                if needed == 0:
                    break
            expected[(reservation_id, consumption.pk if consumption else None)] = allocations

        ambiguous = any(
            r.use_ledger_count != 1
            or int(r.use_ledger_delta or 0) != -int(r.tickets_used)
            or _closed(r)
            for r in missing
        )
        # Applying a replay may also change existing rows, so every touched month must be open.
        touched = [c.reservation for c in active if c.purchase_id]
        ambiguous = ambiguous or any(_closed(r) for r in touched)
        for reservation in missing:
            allocations = expected[(reservation.pk, None)]
            purchase = allocations[0][0] if len(allocations) == 1 and allocations[0][1] == reservation.tickets_used else None
            reason = "formal_fifo_unique" if not ambiguous else "rejected_ledger_or_closed_month"
            rows.append(MissingConsumptionPreview(
                user=_name(user), user_id=user.pk, reservation_id=reservation.pk,
                lesson_date=reservation.start_at.date().isoformat(),
                ledger_consumption_count=reservation.use_ledger_count,
                ticket_consumption_exists=False, current_purchase_id=None,
                expected_purchase_id=purchase.pk if purchase else None,
                current_unit_price=None,
                expected_unit_price=int(purchase.unit_price) if purchase else None,
                purchase_remaining_before=int(purchase.remaining_tickets) if purchase else None,
                purchase_remaining_after_expected=remaining.get(purchase.pk) if purchase else None,
                participant_ticket_price_before=reservation.participant_ticket_price_snapshot,
                participant_ticket_price_after_expected=(int(purchase.unit_price) * reservation.tickets_used if purchase else None),
                candidate=not ambiguous, reason=reason,
            ))
        for consumption in sorted(active, key=lambda row: (row.reservation.ticket_consumed_at, row.reservation_id, row.pk)):
            allocations = expected[(consumption.reservation_id, consumption.pk)]
            purchase = allocations[0][0] if len(allocations) == 1 and allocations[0][1] == consumption.tickets_used else None
            expected_purchase_id = purchase.pk if purchase else None
            expected_price = int(purchase.unit_price) if purchase else None
            if consumption.purchase_id == expected_purchase_id and consumption.unit_price_snapshot == expected_price:
                continue
            reservation = consumption.reservation
            rows.append(MissingConsumptionPreview(
                user=_name(user), user_id=user.pk, reservation_id=reservation.pk,
                lesson_date=reservation.start_at.date().isoformat(),
                ledger_consumption_count=TicketLedger.objects.filter(
                    reservation=reservation, user=user,
                    reason=TicketLedger.REASON_RESERVATION_USE,
                ).count(),
                ticket_consumption_exists=True,
                current_purchase_id=consumption.purchase_id,
                expected_purchase_id=expected_purchase_id,
                current_unit_price=consumption.unit_price_snapshot,
                expected_unit_price=expected_price,
                purchase_remaining_before=(int(purchase.remaining_tickets) if purchase else None),
                purchase_remaining_after_expected=(remaining.get(purchase.pk) if purchase else None),
                participant_ticket_price_before=reservation.participant_ticket_price_snapshot,
                participant_ticket_price_after_expected=(expected_price * consumption.tickets_used if expected_price else None),
                candidate=not ambiguous,
                reason="formal_fifo_reallocation" if not ambiguous else "rejected_ledger_or_closed_month",
            ))
    return rows


@transaction.atomic
def repair_missing_ticket_consumptions(*, user_id):
    User.objects.select_for_update().get(pk=user_id)
    previews = inspect_missing_ticket_consumptions(user_ids=[user_id])
    if not previews:
        return []
    if not all(row.candidate for row in previews):
        raise MissingConsumptionRepairRejected("user repair plan is not uniquely safe")

    reservations = list(Reservation.objects.select_for_update().filter(user_id=user_id))
    earliest_missing_at = min(
        Reservation.objects.get(pk=row.reservation_id).ticket_consumed_at for row in previews
    )
    active = list(
        TicketConsumption.objects.select_for_update().filter(
            user_id=user_id, refunded_at__isnull=True,
            reservation__status=Reservation.STATUS_ACTIVE,
            reservation__ticket_refunded_at__isnull=True,
        ).select_related("reservation")
    )
    active = [
        row for row in active
        if row.purchase_id is None or row.purchase.purchased_at > earliest_missing_at
    ]
    purchases = list(TicketPurchase.objects.select_for_update().filter(
        user_id=user_id, purchased_at__gt=earliest_missing_at
    ).order_by("purchased_at", "id"))
    ledger_before = list(TicketLedger.objects.filter(user_id=user_id).values_list("id", "change_amount", "balance_after"))
    balance_before = User.objects.get(pk=user_id).ticket_balance
    capacity = {p.pk: int(p.remaining_tickets) for p in purchases}
    for row in active:
        if row.purchase_id:
            capacity[row.purchase_id] += int(row.tickets_used)

    missing_ids = {row.reservation_id for row in previews if not row.ticket_consumption_exists}
    for reservation in reservations:
        if reservation.pk in missing_ids:
            active.append(TicketConsumption.objects.create(
                user_id=user_id, reservation=reservation, fixed_lesson_id=reservation.fixed_lesson_id,
                purchase=None, tickets_used=reservation.tickets_used, unit_price_snapshot=None,
            ))
    for row in active:
        row.purchase_id = None
        row.unit_price_snapshot = None
    remaining = capacity.copy()
    for row in sorted(active, key=lambda c: (c.reservation.ticket_consumed_at, c.reservation_id, c.pk)):
        needed = int(row.tickets_used)
        eligible = [p for p in purchases if p.purchased_at > row.reservation.ticket_consumed_at and remaining[p.pk] > 0]
        if eligible and remaining[eligible[0].pk] >= needed:
            purchase = eligible[0]
            row.purchase_id = purchase.pk
            row.unit_price_snapshot = purchase.unit_price
            remaining[purchase.pk] -= needed
        row.save(update_fields=["purchase", "unit_price_snapshot"])
    for purchase in purchases:
        purchase.remaining_tickets = remaining[purchase.pk]
        purchase.save(update_fields=["remaining_tickets"])
    for reservation in Reservation.objects.filter(pk__in={row.reservation_id for row in active}):
        rows = list(reservation.ticket_consumptions.filter(refunded_at__isnull=True))
        if rows and all(row.purchase_id for row in rows):
            set_participant_ticket_price_snapshot(reservation, rows)
        elif reservation.participant_ticket_price_snapshot is not None:
            reservation.participant_ticket_price_snapshot = None
            reservation.save(update_fields=["participant_ticket_price_snapshot"])
    if User.objects.get(pk=user_id).ticket_balance != balance_before or list(TicketLedger.objects.filter(user_id=user_id).values_list("id", "change_amount", "balance_after")) != ledger_before:
        raise MissingConsumptionRepairRejected("ledger or balance changed")
    return inspect_missing_ticket_consumptions(user_ids=[user_id])
