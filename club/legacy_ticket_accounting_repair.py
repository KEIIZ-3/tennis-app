"""Conservatively rebuild legacy reservation ticket accounting from FIFO history."""

from dataclasses import asdict, dataclass
from datetime import date, datetime, time

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Reservation, TicketConsumption, TicketLedger, TicketPurchase, User
from .participant_price_snapshot import ticket_revenue_from_consumptions
from .settlement_models import MonthlySettlement


DEFAULT_FROM_DATE = date(2026, 8, 1)
EXCLUDED_TEST_MONTHS = {(2026, 4), (2026, 5)}


class LegacyTicketAccountingRepairRejected(ValidationError):
    pass


@dataclass(frozen=True)
class RepairResult:
    reservation_id: int
    lesson_date: str
    member_name: str
    coach_name: str
    tickets_used: int
    current_snapshot: int | None
    current_consumptions: list
    candidate_purchases: list
    candidate_unit_prices: list
    expected_snapshot: int | None
    reason: str
    repair_status: str

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


def _event_time(reservation, ledger):
    return reservation.ticket_consumed_at or ledger.created_at


def _current_consumptions(reservation):
    return [
        {
            "id": row.id,
            "purchase_id": row.purchase_id,
            "tickets_used": int(row.tickets_used or 0),
            "unit_price_snapshot": row.unit_price_snapshot,
            "refunded": row.refunded_at is not None,
        }
        for row in reservation.ticket_consumptions.order_by("created_at", "id")
    ]


def _rebuild_user_fifo(user_id):
    """Return reservation allocations only when the persisted history has one meaning."""
    purchases = list(TicketPurchase.objects.filter(user_id=user_id).order_by("purchased_at", "id"))
    if any(row.reversed_at is not None for row in purchases):
        return {}, "reversed_purchase_history"
    ledgers = list(TicketLedger.objects.filter(user_id=user_id).order_by("created_at", "id"))
    if any(row.reason == TicketLedger.REASON_ADMIN_ADJUST for row in ledgers):
        return {}, "manual_balance_history"

    events = []
    for purchase in purchases:
        events.append((purchase.purchased_at, 0, purchase.id, "purchase", purchase))
    reservations = {
        row.id: row
        for row in Reservation.objects.filter(
            ticket_ledgers__user_id=user_id
        ).distinct().select_related("user")
    }
    for ledger in ledgers:
        if ledger.reason in (TicketLedger.REASON_PURCHASE_SINGLE, TicketLedger.REASON_PURCHASE_SET4):
            continue
        if ledger.reason in (TicketLedger.REASON_RESERVATION_USE, TicketLedger.REASON_FIXED_USE):
            if int(ledger.change_amount) >= 0:
                return {}, "invalid_consumption_ledger"
            reservation = reservations.get(ledger.reservation_id)
            when = _event_time(reservation, ledger) if reservation else ledger.created_at
            events.append((when, 1, ledger.id, "consume", ledger))
        elif ledger.reason in (TicketLedger.REASON_CANCEL_REFUND, TicketLedger.REASON_RAIN_REFUND):
            if int(ledger.change_amount) <= 0:
                return {}, "invalid_refund_ledger"
            events.append((ledger.created_at, 2, ledger.id, "refund", ledger))
        elif ledger.reason == TicketLedger.REASON_PURCHASE_REVERSAL:
            return {}, "purchase_reversal_history"

    remaining = {row.id: 0 for row in purchases}
    allocations = {}
    for _, _, _, kind, obj in sorted(events, key=lambda item: item[:3]):
        if kind == "purchase":
            remaining[obj.id] += int(obj.total_tickets)
            continue
        if kind == "consume":
            needed = -int(obj.change_amount)
            used = []
            while needed:
                purchase = next((row for row in purchases if remaining[row.id] > 0), None)
                if purchase is None:
                    return {}, "purchase_history_insufficient"
                purchase_id = purchase.id
                amount = min(needed, remaining[purchase_id])
                remaining[purchase_id] -= amount
                needed -= amount
                used.append((purchase_id, amount))
            if obj.reservation_id:
                if obj.reservation_id in allocations:
                    return {}, "multiple_consumption_ledgers"
                allocations[obj.reservation_id] = used
        else:
            if not obj.reservation_id or obj.reservation_id not in allocations:
                return {}, "refund_without_unique_consumption"
            refunded = allocations[obj.reservation_id]
            if sum(amount for _, amount in refunded) != int(obj.change_amount):
                return {}, "refund_amount_mismatch"
            for purchase_id, amount in refunded:
                remaining[purchase_id] += amount

    if any(remaining[row.id] != int(row.remaining_tickets) for row in purchases):
        return {}, "purchase_remaining_history_mismatch"
    user = User.objects.only("ticket_balance").get(pk=user_id)
    if sum(remaining.values()) != int(user.ticket_balance or 0):
        return {}, "legacy_unknown_balance"
    return allocations, None


def _classify(reservation, allocations, history_error):
    current = _current_consumptions(reservation)
    base = dict(
        reservation_id=reservation.id,
        lesson_date=timezone.localtime(reservation.start_at).date().isoformat(),
        member_name=_name(reservation.user),
        coach_name=_name(reservation.coach) if reservation.coach_id else "",
        tickets_used=int(reservation.tickets_used or 0),
        current_snapshot=reservation.participant_ticket_price_snapshot,
        current_consumptions=current,
        candidate_purchases=[],
        candidate_unit_prices=[],
        expected_snapshot=None,
    )
    local_day = timezone.localtime(reservation.start_at).date()
    if (local_day.year, local_day.month) in EXCLUDED_TEST_MONTHS:
        return RepairResult(**base, reason="developer_test_period", repair_status="excluded_test_period")
    if _closed(reservation):
        return RepairResult(**base, reason="accounting_month_closed", repair_status="skipped_closed_month")
    if reservation.status != Reservation.STATUS_ACTIVE or reservation.ticket_refunded_at is not None:
        return RepairResult(**base, reason="reservation_not_active_consumption", repair_status="ambiguous")
    if int(reservation.tickets_used or 0) <= 0:
        return RepairResult(**base, reason="tickets_used_not_positive", repair_status="already_ok")
    ledgers = list(TicketLedger.objects.filter(
        reservation=reservation, user=reservation.user,
        reason=TicketLedger.REASON_RESERVATION_USE,
    ))
    if len(ledgers) != 1 or int(ledgers[0].change_amount) != -int(reservation.tickets_used):
        return RepairResult(**base, reason="single_exact_reservation_use_ledger_required", repair_status="ambiguous")
    if history_error:
        return RepairResult(**base, reason=history_error, repair_status="ambiguous")
    allocation = allocations.get(reservation.id)
    if not allocation or sum(amount for _, amount in allocation) != int(reservation.tickets_used):
        return RepairResult(**base, reason="fifo_allocation_not_unique", repair_status="ambiguous")
    purchase_map = TicketPurchase.objects.in_bulk([purchase_id for purchase_id, _ in allocation])
    candidates = [
        {"purchase_id": purchase_id, "tickets_used": amount, "unit_price": int(purchase_map[purchase_id].unit_price)}
        for purchase_id, amount in allocation
    ]
    prices = [row["unit_price"] for row in candidates]
    base.update(
        candidate_purchases=candidates,
        candidate_unit_prices=prices,
        expected_snapshot=sum(row["tickets_used"] * row["unit_price"] for row in candidates),
    )
    if any(
        row["unit_price"] == 0
        and purchase_map[row["purchase_id"]].purchase_type != TicketPurchase.PURCHASE_TYPE_FORMAL_FREE
        for row in candidates
    ):
        return RepairResult(**base, reason="zero_price_purchase_not_proven_free", repair_status="ambiguous")
    expected_shape = [(row["purchase_id"], row["tickets_used"], row["unit_price"]) for row in candidates]
    current_shape = [
        (row["purchase_id"], row["tickets_used"], row["unit_price_snapshot"])
        for row in current if not row["refunded"]
    ]
    if current_shape == expected_shape and reservation.participant_ticket_price_snapshot == base["expected_snapshot"]:
        return RepairResult(**base, reason="accounting_already_consistent", repair_status="already_ok")
    if reservation.participant_ticket_price_snapshot is not None:
        return RepairResult(**base, reason="existing_snapshot_outside_repair_scope", repair_status="ambiguous")
    if any(row["refunded"] for row in current) or (current and len(current) != len(candidates)):
        return RepairResult(**base, reason="existing_consumption_shape_conflict", repair_status="ambiguous")
    return RepairResult(**base, reason="fifo_history_uniquely_reconstructed", repair_status="repairable")


def inspect_legacy_ticket_accounting(*, from_date=DEFAULT_FROM_DATE, to_date=None, reservation_ids=None):
    start = timezone.make_aware(datetime.combine(from_date, time.min))
    queryset = Reservation.objects.filter(
        start_at__gte=start, tickets_used__gt=0, user_id__isnull=False
    ).select_related("user", "coach").order_by("start_at", "id")
    if to_date:
        end = timezone.make_aware(datetime.combine(to_date, time.max))
        queryset = queryset.filter(start_at__lte=end)
    if reservation_ids:
        queryset = queryset.filter(pk__in=reservation_ids)
    rows = list(queryset)
    cache = {}
    results = []
    for reservation in rows:
        if reservation.user_id is None:
            continue
        if reservation.user_id not in cache:
            cache[reservation.user_id] = _rebuild_user_fifo(reservation.user_id)
        results.append(_classify(reservation, *cache[reservation.user_id]))
    return results


@transaction.atomic
def apply_legacy_ticket_accounting(*, from_date=DEFAULT_FROM_DATE, to_date=None, reservation_ids=None):
    results = inspect_legacy_ticket_accounting(from_date=from_date, to_date=to_date, reservation_ids=reservation_ids)
    user_ids = list(Reservation.objects.filter(
        pk__in=[row.reservation_id for row in results]
    ).values_list("user_id", flat=True).distinct())
    list(User.objects.select_for_update().filter(pk__in=user_ids).order_by("id"))
    results = inspect_legacy_ticket_accounting(from_date=from_date, to_date=to_date, reservation_ids=reservation_ids)
    repairable = [row for row in results if row.repair_status == "repairable"]
    changed_months = set()
    for result in repairable:
        reservation = Reservation.objects.select_for_update().get(pk=result.reservation_id)
        existing = list(reservation.ticket_consumptions.select_for_update().order_by("created_at", "id"))
        for index, candidate in enumerate(result.candidate_purchases):
            values = dict(
                user_id=reservation.user_id,
                purchase_id=candidate["purchase_id"],
                reservation=reservation,
                fixed_lesson_id=reservation.fixed_lesson_id,
                tickets_used=candidate["tickets_used"],
                unit_price_snapshot=candidate["unit_price"],
            )
            if index < len(existing):
                row = existing[index]
                for field, value in values.items():
                    setattr(row, field, value)
                row.save(update_fields=["user", "purchase", "reservation", "fixed_lesson", "tickets_used", "unit_price_snapshot"])
            else:
                TicketConsumption.objects.create(**values)
        consumptions = list(reservation.ticket_consumptions.filter(refunded_at__isnull=True))
        snapshot = ticket_revenue_from_consumptions(consumptions)
        if snapshot != result.expected_snapshot:
            raise LegacyTicketAccountingRepairRejected("snapshot_reconstruction_mismatch")
        reservation.participant_ticket_price_snapshot = snapshot
        reservation.save(update_fields=["participant_ticket_price_snapshot"])
        changed_months.add((reservation.start_at.year, reservation.start_at.month))

    from .settlement_service import calculate_monthly_settlement
    for year, month in sorted(changed_months):
        settlement = MonthlySettlement.objects.filter(year=year, month=month).first()
        if settlement is None or not settlement.is_closed:
            calculate_monthly_settlement(year, month, force=True)
    return repairable
