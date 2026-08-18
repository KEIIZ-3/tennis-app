"""Repair approved historical consumption evidence without replaying ticket use."""

from dataclasses import asdict, dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Sum

from .models import Reservation, TicketConsumption, TicketLedger, TicketPurchase, User
from .participant_price_snapshot import set_participant_ticket_price_snapshot
from .settlement_models import MonthlySettlement


CONFIRMED_PRICE_WITHOUT_PURCHASE = {1525: 3500}


class HistoricalRepairRejected(ValidationError):
    pass


@dataclass(frozen=True)
class HistoricalRepairPreview:
    reservation_id: int
    participant_name: str
    ticket_balance_before: int
    ticket_balance_after_expected: int
    ledger_count_before: int
    ledger_delta_before: int
    ledger_count_after_expected: int
    ledger_delta_after_expected: int
    existing_consumption_count: int
    candidate_purchase_id: int | None
    candidate_purchase_unit_price: int | None
    repair_mode: str
    purchase_remaining_before: int | None
    purchase_remaining_after_expected: int | None
    participant_ticket_price_before: int | None
    participant_ticket_price_after_expected: int | None
    will_change_balance: bool = False
    will_create_ledger: bool = False
    will_change_wallet: bool = False
    will_change_court_settlement: bool = False
    candidate: bool = False
    status: str = "rejected"
    reason: str = ""

    def to_dict(self):
        return asdict(self)


def _participant_name(reservation):
    participant = getattr(reservation, "participant_snapshot", None)
    return (
        getattr(participant, "participant_name", "")
        or reservation.user.full_name
        or reservation.user.username
    )


def _ledger_state(user_id):
    state = TicketLedger.objects.filter(user_id=user_id).aggregate(
        count=Count("id"), delta=Sum("change_amount")
    )
    return int(state["count"] or 0), int(state["delta"] or 0)


def _month_is_closed(reservation):
    return MonthlySettlement.objects.filter(
        year=reservation.start_at.year,
        month=reservation.start_at.month,
        status=MonthlySettlement.STATUS_CLOSED,
    ).exists()


def inspect_historical_ticket_consumption_repair(
    reservation_id, *, candidate_purchase_id=None, confirmed_unit_price=None, lock=False
):
    reservations = Reservation.objects.select_related("user")
    if lock:
        reservations = reservations.select_for_update()
    reservation = reservations.get(pk=reservation_id)
    balance = int(reservation.user.ticket_balance or 0)
    ledger_count, ledger_delta = _ledger_state(reservation.user_id)
    consumption_count = TicketConsumption.objects.filter(
        reservation_id=reservation.id
    ).count()
    mode = "confirmed_price_without_purchase" if candidate_purchase_id is None else "purchase_linkage"
    price = confirmed_unit_price
    purchase = None
    if candidate_purchase_id is not None:
        purchases = TicketPurchase.objects
        if lock:
            purchases = purchases.select_for_update()
        purchase = purchases.filter(pk=candidate_purchase_id).first()
        price = int(purchase.unit_price) if purchase else None
    remaining_before = int(purchase.remaining_tickets) if purchase else None
    tickets_used = int(reservation.tickets_used or 0)
    price_after = None if price is None else int(price) * tickets_used
    base = dict(
        reservation_id=reservation.id,
        participant_name=_participant_name(reservation),
        ticket_balance_before=balance,
        ticket_balance_after_expected=balance,
        ledger_count_before=ledger_count,
        ledger_delta_before=ledger_delta,
        ledger_count_after_expected=ledger_count,
        ledger_delta_after_expected=ledger_delta,
        existing_consumption_count=consumption_count,
        candidate_purchase_id=candidate_purchase_id,
        candidate_purchase_unit_price=price,
        repair_mode=mode,
        purchase_remaining_before=remaining_before,
        purchase_remaining_after_expected=(
            None if remaining_before is None else remaining_before - tickets_used
        ),
        participant_ticket_price_before=reservation.participant_ticket_price_snapshot,
        participant_ticket_price_after_expected=price_after,
    )
    if consumption_count:
        return HistoricalRepairPreview(**base, status="noop", reason="already_repaired")
    if reservation.ticket_consumed_at is None:
        return HistoricalRepairPreview(**base, reason="ticket_consumed_at_missing")
    if tickets_used <= 0:
        return HistoricalRepairPreview(**base, reason="tickets_used_not_positive")
    ledgers = TicketLedger.objects.filter(
        reservation_id=reservation.id,
        user_id=reservation.user_id,
        reason=TicketLedger.REASON_RESERVATION_USE,
        change_amount=-tickets_used,
    )
    if ledgers.count() != 1:
        return HistoricalRepairPreview(**base, reason="single_reservation_use_ledger_required")
    if _month_is_closed(reservation):
        return HistoricalRepairPreview(**base, reason="accounting_month_closed")
    if reservation.participant_ticket_price_snapshot not in (None, price_after):
        return HistoricalRepairPreview(**base, reason="participant_price_snapshot_conflict")
    if candidate_purchase_id is None:
        approved_price = CONFIRMED_PRICE_WITHOUT_PURCHASE.get(reservation.id)
        if approved_price is None or int(confirmed_unit_price or 0) != approved_price:
            return HistoricalRepairPreview(**base, reason="confirmed_price_evidence_required")
    else:
        if purchase is None:
            return HistoricalRepairPreview(**base, reason="candidate_purchase_not_found")
        if purchase.user_id != reservation.user_id:
            return HistoricalRepairPreview(**base, reason="candidate_purchase_user_mismatch")
        if purchase.purchased_at <= reservation.ticket_consumed_at:
            return HistoricalRepairPreview(**base, reason="candidate_purchase_not_later")
        if int(price or 0) <= 0:
            return HistoricalRepairPreview(**base, reason="candidate_purchase_price_missing")
        if remaining_before < tickets_used:
            return HistoricalRepairPreview(**base, reason="candidate_purchase_capacity_insufficient")
    return HistoricalRepairPreview(
        **base, candidate=True, status="candidate", reason="historical_evidence_confirmed"
    )


def repair_historical_ticket_consumption(
    reservation_id, *, candidate_purchase_id=None, confirmed_unit_price=None
):
    """Create evidence and allocate lot capacity; never changes balance or ledgers."""
    with transaction.atomic():
        reservation = Reservation.objects.select_for_update().get(pk=reservation_id)
        User.objects.select_for_update().get(pk=reservation.user_id)
        list(
            TicketConsumption.objects.select_for_update().filter(
                reservation_id=reservation_id
            )
        )
        preview = inspect_historical_ticket_consumption_repair(
            reservation_id,
            candidate_purchase_id=candidate_purchase_id,
            confirmed_unit_price=confirmed_unit_price,
            lock=True,
        )
        if preview.status == "noop":
            return preview
        if not preview.candidate:
            raise HistoricalRepairRejected(preview.reason)

        balance_before = int(User.objects.get(pk=reservation.user_id).ticket_balance or 0)
        ledger_before = _ledger_state(reservation.user_id)
        settlement_before = list(
            MonthlySettlement.objects.filter(
                year=reservation.start_at.year, month=reservation.start_at.month
            ).values()
        )
        reservation_accounting_before = Reservation.objects.filter(pk=reservation_id).values(
            "court_id", "start_at", "end_at", "payment_method", "payment_status",
            "payment_amount", "payment_received_at",
        ).get()
        purchase = None
        if candidate_purchase_id is not None:
            purchase = TicketPurchase.objects.select_for_update().get(pk=candidate_purchase_id)
            purchase.remaining_tickets -= int(reservation.tickets_used)
            purchase.save(update_fields=["remaining_tickets"])

        consumption = TicketConsumption.objects.create(
            user_id=reservation.user_id,
            purchase=purchase,
            reservation=reservation,
            fixed_lesson_id=reservation.fixed_lesson_id,
            tickets_used=reservation.tickets_used,
            unit_price_snapshot=preview.candidate_purchase_unit_price,
        )
        set_participant_ticket_price_snapshot(reservation, [consumption])

        if int(User.objects.get(pk=reservation.user_id).ticket_balance or 0) != balance_before:
            raise HistoricalRepairRejected("ticket_balance_changed")
        if _ledger_state(reservation.user_id) != ledger_before:
            raise HistoricalRepairRejected("ticket_ledger_changed")
        if list(MonthlySettlement.objects.filter(
            year=reservation.start_at.year, month=reservation.start_at.month
        ).values()) != settlement_before:
            raise HistoricalRepairRejected("wallet_or_settlement_changed")
        if Reservation.objects.filter(pk=reservation_id).values(
            "court_id", "start_at", "end_at", "payment_method", "payment_status",
            "payment_amount", "payment_received_at",
        ).get() != reservation_accounting_before:
            raise HistoricalRepairRejected("reservation_accounting_changed")
        return HistoricalRepairPreview(
            **{**preview.to_dict(), "status": "repaired", "reason": "historical_evidence_restored"}
        )
