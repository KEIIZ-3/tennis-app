"""Reservation-first, SELECT-only audit of executed lesson ticket evidence."""

from collections import defaultdict
from datetime import date, timedelta

from django.db.models import Prefetch
from django.utils import timezone

from . import lesson_execution
from .lesson_execution_storage import read_status_map
from .models import Reservation, TicketConsumption, TicketLedger, TicketPurchase
from .settlement_balance_policy import _execution_slot_key
from .settlement_models import MonthlySettlement
from .settlement_wallet_audit import _display_name
from .ticket_consumption_audit import classify_purchase


def _date_range(year, month, through_day):
    start = date(int(year), int(month), 1)
    if through_day is not None:
        end = date(int(year), int(month), int(through_day)) + timedelta(days=1)
    elif int(month) == 12:
        end = date(int(year) + 1, 1, 1)
    else:
        end = date(int(year), int(month) + 1, 1)
    return start, end


def _consumption_details(consumptions):
    return [
        {
            "consumption_id": item.pk,
            "purchase_id": item.purchase_id,
            "purchase_type": item.purchase.purchase_type if item.purchase_id else None,
            "classification": classify_purchase(item.purchase) if item.purchase_id else "pending_purchase",
            "tickets_used": int(item.tickets_used or 0),
            "unit_price_snapshot": int(item.unit_price_snapshot or 0),
            "consumption_value": int(item.tickets_used or 0) * int(item.unit_price_snapshot or 0),
            "refunded_at": item.refunded_at.isoformat() if item.refunded_at else None,
            "returned": item.refunded_at is not None,
        }
        for item in consumptions
    ]


def _integrity_classification(reservation, details):
    active = [row for row in details if not row["returned"]]
    if details and not active:
        return "refunded", "TicketConsumptionは存在するが全件返却済み"
    if int(reservation.tickets_used or 0) == 0:
        if details:
            return "other_inconsistency", "tickets_used=0だがTicketConsumptionが存在"
        return "zero_ticket", "Reservation.tickets_used=0"
    if not details:
        evidence = []
        if reservation.ticket_consumed_at:
            evidence.append("ticket_consumed_at")
        if list(reservation.ticket_ledgers.all()):
            evidence.append("TicketLedger")
        reason = "TicketConsumption欠落"
        if evidence:
            reason += "（消費証拠: " + ", ".join(evidence) + "）"
        else:
            reason += "（実消費を示す保存証拠なし）"
        return "missing_consumption", reason
    classes = {row["classification"] for row in active}
    consumed_tickets = sum(row["tickets_used"] for row in active)
    if consumed_tickets != int(reservation.tickets_used or 0):
        return "other_inconsistency", "有効Consumption枚数とReservation.tickets_usedが不一致"
    if len(classes) != 1:
        return "other_inconsistency", "複数種別の有効Consumptionが混在"
    classification = next(iter(classes))
    if classification in {"paid", "formal_free", "legacy"}:
        return classification, f"{classification} Consumption正常"
    if classification == "adjustment":
        return "adjustment_free", "admin/adjustment Consumption"
    return "unknown", "分類不能なPurchase種別"


def audit_executed_reservation_ticket_integrity(year, month, *, through_day=None, now=None):
    """Audit held participants from Reservation without changing database state."""
    start, end = _date_range(year, month, through_day)
    current_time = now or timezone.now()
    settlement = MonthlySettlement.objects.filter(year=year, month=month).first()
    status_map = read_status_map(settlement) if settlement else {}
    candidates = list(
        Reservation.objects.filter(start_at__date__gte=start, start_at__date__lt=end)
        .select_related(
            "user", "coach", "substitute_coach", "availability", "fixed_lesson",
            "participant_snapshot",
        )
        .prefetch_related(
            Prefetch("ticket_consumptions", queryset=TicketConsumption.objects.select_related("purchase").order_by("id")),
            Prefetch("ticket_ledgers", queryset=TicketLedger.objects.order_by("id")),
        )
        .order_by("start_at", "id")
    )
    occurrences = defaultdict(list)
    for reservation in candidates:
        occurrences[_execution_slot_key(reservation)].append(reservation)

    rows = []
    for reservation in candidates:
        occurrence = _execution_slot_key(reservation)
        status, _cancellation_type = lesson_execution.effective_status(
            status_map.get(occurrence), occurrences[occurrence],
            end_at=reservation.end_at, now=current_time,
        )
        if status != lesson_execution.STATUS_HELD or reservation.status != Reservation.STATUS_ACTIVE:
            continue
        consumptions = list(reservation.ticket_consumptions.all())
        details = _consumption_details(consumptions)
        integrity_classification, integrity_reason = _integrity_classification(reservation, details)
        participant = getattr(reservation, "participant_snapshot", None)
        ledgers = list(reservation.ticket_ledgers.all())
        snapshot = reservation.participant_ticket_price_snapshot
        rows.append({
            "reservation_id": reservation.pk,
            "user_id": reservation.user_id,
            "member_name": _display_name(reservation.user),
            "participant_name": getattr(participant, "participant_name", "") or _display_name(reservation.user),
            "participant_type": getattr(participant, "participant_type", "self"),
            "lesson_date": timezone.localtime(reservation.start_at).date().isoformat() if timezone.is_aware(reservation.start_at) else reservation.start_at.date().isoformat(),
            "start_at": reservation.start_at.isoformat(),
            "end_at": reservation.end_at.isoformat(),
            "lesson_datetime": f"{reservation.start_at.isoformat()} / {reservation.end_at.isoformat()}",
            "occurrence": occurrence,
            "coach": _display_name(reservation.coach),
            "substitute_coach": _display_name(reservation.substitute_coach),
            "lesson_type": reservation.lesson_type,
            "reservation_status": reservation.status,
            "execution_status": status,
            "reservation_tickets_used": int(reservation.tickets_used or 0),
            "reservation_ticket_consumed_at": reservation.ticket_consumed_at.isoformat() if reservation.ticket_consumed_at else None,
            "reservation_ticket_refunded_at": reservation.ticket_refunded_at.isoformat() if reservation.ticket_refunded_at else None,
            "participant_ticket_price_snapshot": snapshot,
            "ball_expense_eligible": snapshot is not None and int(snapshot) > 1000,
            "ball_expense_reason": "保存単価が1000円超" if snapshot is not None and int(snapshot) > 1000 else ("保存単価が1000円以下" if snapshot is not None else "保存単価なしのため判定不能"),
            "consumption_ids": [row["consumption_id"] for row in details],
            "consumption_count": len(details),
            "consumption_tickets": sum(row["tickets_used"] for row in details),
            "consumption_unit_price": [row["unit_price_snapshot"] for row in details],
            "consumption_value": sum(row["consumption_value"] for row in details if not row["returned"]),
            "classification": sorted({row["classification"] for row in details}),
            "returned": bool(details) and all(row["returned"] for row in details),
            "consumptions": details,
            "ledger_evidence": [{"ledger_id": ledger.pk, "change_amount": ledger.change_amount, "reason": ledger.reason, "balance_after": ledger.balance_after, "created_at": ledger.created_at.isoformat()} for ledger in ledgers],
            "purchase_evidence": [{"purchase_id": row["purchase_id"], "purchase_type": row["purchase_type"], "unit_price_snapshot": row["unit_price_snapshot"]} for row in details],
            "integrity_classification": integrity_classification,
            "integrity_reason": integrity_reason,
        })

    summary = {
        "executed_participant_count": len(rows), "reservation_count": len(rows),
        "with_consumption_count": sum(bool(row["consumption_count"]) for row in rows),
        "without_consumption_count": sum(not row["consumption_count"] for row in rows),
        "paid_count": 0, "formal_free_count": 0, "adjustment_free_count": 0,
        "legacy_count": 0, "refunded_consumption_count": 0, "zero_ticket_count": 0,
        "unknown_count": 0,
    }
    key_map = {"paid": "paid_count", "formal_free": "formal_free_count", "adjustment_free": "adjustment_free_count", "legacy": "legacy_count", "refunded": "refunded_consumption_count", "zero_ticket": "zero_ticket_count"}
    for row in rows:
        key = key_map.get(row["integrity_classification"], "unknown_count")
        summary[key] += 1
    missing = [row for row in rows if row["integrity_classification"] == "missing_consumption"]
    summary.update({
        "current_executed_revenue": sum(row["consumption_value"] for row in rows if row["integrity_classification"] == "paid"),
        "recoverable_missing_revenue": sum(int(row["participant_ticket_price_snapshot"] or 0) for row in missing if row["participant_ticket_price_snapshot"] is not None),
        "unknown_missing_revenue_count": sum(row["participant_ticket_price_snapshot"] is None for row in missing),
        "ball_expense_unknown_due_to_missing_consumption_count": sum(row["participant_ticket_price_snapshot"] is None and row["reservation_tickets_used"] > 0 for row in missing),
        "court_expense_affected": False,
        "wallet_cash_affected": False,
    })
    return {"year": int(year), "month": int(month), "through_day": through_day, "summary": summary, "reservations": rows}
