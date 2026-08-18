"""Repair missing participant prices from complete persisted ticket evidence only."""

from dataclasses import asdict, dataclass

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from .models import Court, Reservation, TicketConsumption, TicketLedger, TicketPurchase, User
from .participant_price_snapshot import is_ball_expense_eligible
from .lesson_execution_storage import read_status_map
from .settlement_balance_policy import _execution_slot_key
from .settlement_models import (
    CoachMonthlySettlement, ExpenseSettlementAllocation, MonthlySettlement, SettlementPayment,
)


class ParticipantPriceSnapshotRepairRejected(ValidationError):
    pass


@dataclass(frozen=True)
class ParticipantPriceSnapshotRepairPreview:
    reservation_id: int
    participant_name: str
    start_at: str
    status: str
    execution_status: str
    tickets_used: int
    consumption_ids: list
    purchase_ids: list
    consumption_prices: list
    consumption_tickets: list
    snapshot_before: int | None
    snapshot_after_expected: int | None
    ball_expense_eligible_before: bool
    ball_expense_eligible_after_expected: bool | None
    will_change_ticket_balance: bool = False
    will_change_ledger: bool = False
    will_change_purchase: bool = False
    will_change_consumption: bool = False
    will_change_wallet: bool = False
    will_change_court: bool = False
    candidate: bool = False
    reason: str = ""

    def to_dict(self):
        return asdict(self)


def _participant_name(reservation):
    try:
        name = reservation.participant_snapshot.participant_name
    except (AttributeError, ObjectDoesNotExist):
        name = ""
    return name or reservation.user.full_name or reservation.user.username


def _execution_status(reservation):
    settlement = MonthlySettlement.objects.filter(
        year=reservation.start_at.year, month=reservation.start_at.month,
    ).first()
    saved = (
        (read_status_map(settlement).get(_execution_slot_key(reservation)) or {}).get("status")
        if settlement else None
    )
    if saved:
        return saved
    return "scheduled" if reservation.end_at > timezone.now() else "unconfirmed"


def inspect_participant_price_snapshot_repair(reservation_id, *, lock=False):
    query = Reservation.objects.select_related("user")
    if lock:
        query = query.select_for_update()
    reservation = query.get(pk=reservation_id)
    rows_query = reservation.ticket_consumptions.select_related("purchase").order_by("id")
    if lock:
        rows_query = rows_query.select_for_update()
    rows = list(rows_query)
    prices = [row.unit_price_snapshot for row in rows]
    purchase_ids = [row.purchase_id for row in rows]
    total = None
    base = dict(
        reservation_id=reservation.pk,
        participant_name=_participant_name(reservation),
        start_at=reservation.start_at.isoformat(),
        status=reservation.status,
        execution_status=_execution_status(reservation),
        tickets_used=int(reservation.tickets_used or 0),
        consumption_ids=[row.pk for row in rows],
        purchase_ids=purchase_ids,
        consumption_prices=prices,
        consumption_tickets=[int(row.tickets_used or 0) for row in rows],
        snapshot_before=reservation.participant_ticket_price_snapshot,
        snapshot_after_expected=None,
        ball_expense_eligible_before=is_ball_expense_eligible(reservation),
        ball_expense_eligible_after_expected=None,
    )
    if reservation.participant_ticket_price_snapshot is not None:
        return ParticipantPriceSnapshotRepairPreview(**base, reason="snapshot_already_set")
    if int(reservation.tickets_used or 0) <= 0:
        return ParticipantPriceSnapshotRepairPreview(**base, reason="tickets_used_not_positive")
    if not rows:
        return ParticipantPriceSnapshotRepairPreview(**base, reason="consumption_missing")
    if any(row.refunded_at is not None for row in rows):
        return ParticipantPriceSnapshotRepairPreview(**base, reason="refunded_consumption")
    if sum(int(row.tickets_used or 0) for row in rows) != int(reservation.tickets_used):
        return ParticipantPriceSnapshotRepairPreview(**base, reason="consumption_ticket_count_mismatch")
    if any(row.purchase_id is None or row.unit_price_snapshot is None for row in rows):
        return ParticipantPriceSnapshotRepairPreview(**base, reason="incomplete_price_evidence")
    if any(int(row.purchase.unit_price) != int(row.unit_price_snapshot) for row in rows):
        return ParticipantPriceSnapshotRepairPreview(**base, reason="purchase_consumption_price_conflict")
    distinct_prices = {int(price) for price in prices}
    if len(distinct_prices) != 1:
        return ParticipantPriceSnapshotRepairPreview(**base, reason="mixed_prices")
    if distinct_prices == {0}:
        return ParticipantPriceSnapshotRepairPreview(**base, reason="zero_price_excluded")
    if reservation.payment_status == Reservation.PAYMENT_STATUS_WAIVED:
        return ParticipantPriceSnapshotRepairPreview(**base, reason="waived_rule_conflict")
    if int(reservation.custom_ticket_price or 0) > 0 or reservation.is_preopen_cash_lesson():
        return ParticipantPriceSnapshotRepairPreview(**base, reason="special_price_rule_conflict")
    total = sum(int(row.unit_price_snapshot) * int(row.tickets_used) for row in rows)
    return ParticipantPriceSnapshotRepairPreview(
        **{**base, "snapshot_after_expected": total,
           "ball_expense_eligible_after_expected": total > 1000},
        candidate=True, reason="complete_saved_consumption_evidence",
    )


def _protected_state(reservation):
    ledger = TicketLedger.objects.filter(user_id=reservation.user_id).aggregate(
        count=Count("id"), total=Sum("change_amount")
    )
    return {
        "ticket_balance": User.objects.values_list("ticket_balance", flat=True).get(pk=reservation.user_id),
        "ledger": (int(ledger["count"] or 0), int(ledger["total"] or 0)),
        "purchases": list(TicketPurchase.objects.filter(user_id=reservation.user_id).order_by("id").values()),
        "consumptions": list(TicketConsumption.objects.filter(user_id=reservation.user_id).order_by("id").values()),
        "court": list(Court.objects.filter(pk=reservation.court_id).values()),
        "settlements": [
            list(model.objects.order_by("id").values()) for model in (
                MonthlySettlement, CoachMonthlySettlement, SettlementPayment,
                ExpenseSettlementAllocation,
            )
        ],
        "reservation": Reservation.objects.filter(pk=reservation.pk).values(
            "tickets_used", "ticket_consumed_at", "ticket_refunded_at", "payment_status",
            "payment_amount", "court_id", "start_at", "end_at",
        ).get(),
    }


def repair_participant_price_snapshot(reservation_id):
    with transaction.atomic():
        reservation = Reservation.objects.select_for_update().get(pk=reservation_id)
        before = _protected_state(reservation)
        preview = inspect_participant_price_snapshot_repair(reservation_id, lock=True)
        if not preview.candidate:
            return preview
        updated = Reservation.objects.filter(
            pk=reservation_id, participant_ticket_price_snapshot__isnull=True,
        ).update(participant_ticket_price_snapshot=preview.snapshot_after_expected)
        if updated != 1 or _protected_state(reservation) != before:
            raise ParticipantPriceSnapshotRepairRejected("protected_state_changed")
        return ParticipantPriceSnapshotRepairPreview(
            **{**preview.to_dict(), "reason": "snapshot_repaired"}
        )
