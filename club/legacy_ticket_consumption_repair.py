"""Restore missing historical consumption linkage without replaying ticket use."""

from dataclasses import asdict, dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Sum

from .models import Reservation, TicketConsumption, TicketLedger, TicketPurchase, User
from .settlement_models import MonthlySettlement


class RepairRejected(ValidationError):
    pass


@dataclass(frozen=True)
class RepairPreview:
    reservation_id: int
    user_id: int
    participant_name: str
    ticket_balance_before: int
    ticket_balance_after_expected: int
    ledger_delta_before: int
    ledger_delta_after_expected: int
    candidate_purchase_id: int | None
    candidate_unit_price: int | None
    new_consumption_tickets: int
    new_consumption_value: int | None
    will_change_balance: bool = False
    will_create_ledger: bool = False
    will_change_wallet: bool = False
    will_change_court_settlement: bool = False
    candidate: bool = False
    status: str = "rejected"
    reason: str = ""

    def to_dict(self):
        return asdict(self)


def _ledger_state(user_id):
    state = TicketLedger.objects.filter(user_id=user_id).aggregate(
        count=Count("id"), delta=Sum("change_amount"),
    )
    return int(state["count"] or 0), int(state["delta"] or 0)


def _external_accounting_state(reservation_id):
    reservation = Reservation.objects.get(pk=reservation_id)
    reservation_values = Reservation.objects.filter(pk=reservation_id).values(
        "court_id", "start_at", "end_at", "payment_method", "payment_status",
        "payment_amount", "payment_received_at",
    ).get()
    settlement_values = list(MonthlySettlement.objects.filter(
        year=reservation.start_at.year, month=reservation.start_at.month,
    ).order_by("id").values())
    return reservation_values, settlement_values


def _participant_name(reservation):
    try:
        participant = reservation.participant_snapshot.participant_name
    except Reservation.participant_snapshot.RelatedObjectDoesNotExist:
        participant = ""
    return participant or reservation.user.full_name or reservation.user.username


def candidate_purchases(reservation, consumption_ledger):
    """Find lots whose persisted depletion has exactly one unexplained use."""
    candidates = []
    purchases = TicketPurchase.objects.filter(
        user_id=reservation.user_id,
        purchased_at__lte=consumption_ledger.created_at,
    ).order_by("purchased_at", "id")
    for purchase in purchases:
        accounted = TicketConsumption.objects.filter(
            purchase_id=purchase.id, refunded_at__isnull=True,
        ).aggregate(total=Sum("tickets_used"))["total"] or 0
        unexplained = int(purchase.total_tickets) - int(purchase.remaining_tickets) - int(accounted)
        if unexplained == int(reservation.tickets_used):
            candidates.append(purchase)
    return candidates


def inspect_legacy_ticket_consumption_repair(reservation_id, *, lock=False):
    queryset = (
        Reservation.objects.select_for_update()
        if lock
        else Reservation.objects.select_related("user")
    )
    reservation = queryset.get(pk=reservation_id)
    balance = int(reservation.user.ticket_balance or 0)
    ledger_count, ledger_delta = _ledger_state(reservation.user_id)
    base = dict(
        reservation_id=reservation.id,
        user_id=reservation.user_id,
        participant_name=_participant_name(reservation),
        ticket_balance_before=balance,
        ticket_balance_after_expected=balance,
        ledger_delta_before=ledger_delta,
        ledger_delta_after_expected=ledger_delta,
        candidate_purchase_id=None,
        candidate_unit_price=None,
        new_consumption_tickets=int(reservation.tickets_used or 0),
        new_consumption_value=None,
    )

    existing = TicketConsumption.objects.filter(reservation_id=reservation.id)
    if existing.exists():
        return RepairPreview(**base, status="noop", reason="ticket_consumption_already_exists")
    if reservation.ticket_consumed_at is None:
        return RepairPreview(**base, reason="ticket_consumed_at_missing")
    if int(reservation.tickets_used or 0) <= 0:
        return RepairPreview(**base, reason="tickets_used_not_positive")
    ledgers = list(TicketLedger.objects.filter(
        reservation_id=reservation.id,
        user_id=reservation.user_id,
        reason=TicketLedger.REASON_RESERVATION_USE,
        change_amount=-int(reservation.tickets_used),
    ).order_by("id"))
    if len(ledgers) != 1:
        return RepairPreview(**base, reason="single_reservation_use_ledger_required")

    candidates = candidate_purchases(reservation, ledgers[0])
    if len(candidates) != 1:
        return RepairPreview(**base, reason="unique_purchase_lot_required")
    purchase = candidates[0]
    base.update(
        candidate_purchase_id=purchase.id,
        candidate_unit_price=int(purchase.unit_price),
        new_consumption_value=int(purchase.unit_price) * int(reservation.tickets_used),
    )
    if int(purchase.unit_price or 0) <= 0:
        return RepairPreview(**base, reason="unit_price_evidence_missing")
    if int(reservation.participant_ticket_price_snapshot or 0) != base["new_consumption_value"]:
        return RepairPreview(**base, reason="reservation_price_evidence_mismatch")
    return RepairPreview(**base, candidate=True, status="candidate", reason="persisted_evidence_consistent")


def _assert_invariants(*, user_id, purchase_id, reservation_id, balance_before,
                       ledger_before, purchase_remaining_before, external_before):
    balance_after = int(User.objects.get(pk=user_id).ticket_balance or 0)
    ledger_after = _ledger_state(user_id)
    remaining_after = int(TicketPurchase.objects.get(pk=purchase_id).remaining_tickets)
    if balance_after != balance_before:
        raise RepairRejected("ticket_balance_changed")
    if ledger_after != ledger_before:
        raise RepairRejected("ticket_ledger_changed")
    if remaining_after != purchase_remaining_before:
        raise RepairRejected("purchase_remaining_tickets_changed")
    if _external_accounting_state(reservation_id) != external_before:
        raise RepairRejected("wallet_or_court_settlement_changed")


def repair_legacy_ticket_consumption(reservation_id):
    """Create linkage only; never calls Reservation.consume_tickets()."""
    with transaction.atomic():
        preview = inspect_legacy_ticket_consumption_repair(reservation_id, lock=True)
        if preview.status == "noop":
            return preview
        if not preview.candidate:
            raise RepairRejected(preview.reason)

        User.objects.select_for_update().get(pk=preview.user_id)
        locked_purchases = list(TicketPurchase.objects.select_for_update().filter(
            user_id=preview.user_id
        ).order_by("id"))
        purchase = next(
            (row for row in locked_purchases if row.id == preview.candidate_purchase_id),
            None,
        )
        if purchase is None:
            raise RepairRejected("candidate_purchase_disappeared")
        # Re-evaluate after all relevant rows are locked to close the dry-run/apply race.
        preview = inspect_legacy_ticket_consumption_repair(reservation_id, lock=True)
        if not preview.candidate:
            raise RepairRejected(preview.reason)
        ledger_before = _ledger_state(preview.user_id)
        balance_before = int(User.objects.get(pk=preview.user_id).ticket_balance or 0)
        remaining_before = int(purchase.remaining_tickets)
        external_before = _external_accounting_state(preview.reservation_id)

        TicketConsumption.objects.create(
            user_id=preview.user_id,
            purchase_id=preview.candidate_purchase_id,
            reservation_id=preview.reservation_id,
            fixed_lesson_id=Reservation.objects.only("fixed_lesson_id").get(
                pk=preview.reservation_id
            ).fixed_lesson_id,
            tickets_used=preview.new_consumption_tickets,
            unit_price_snapshot=preview.candidate_unit_price,
        )
        _assert_invariants(
            user_id=preview.user_id,
            purchase_id=preview.candidate_purchase_id,
            reservation_id=preview.reservation_id,
            balance_before=balance_before,
            ledger_before=ledger_before,
            purchase_remaining_before=remaining_before,
            external_before=external_before,
        )
        return RepairPreview(**{
            **preview.to_dict(), "status": "repaired", "reason": "linkage_restored"
        })
