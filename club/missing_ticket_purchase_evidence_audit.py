"""SELECT-only evidence audit for missing historical TicketConsumption rows."""

from django.db.models import Sum

from .legacy_ticket_consumption_repair import candidate_purchases, inspect_legacy_ticket_consumption_repair
from .models import Reservation, TicketConsumption, TicketLedger, TicketPurchase, User

DEFAULT_RESERVATION_IDS = (1505, 1506, 1523, 1525, 1498, 1501, 1493, 1503, 1504, 1526, 1531, 1541, 1552, 1553, 1495)


def _iso(value):
    return value.isoformat() if value else None


def _purchase_row(purchase):
    return {"purchase_id": purchase.id, "purchase_type": purchase.purchase_type,
            "total_tickets": int(purchase.total_tickets), "remaining_tickets": int(purchase.remaining_tickets),
            "unit_price": int(purchase.unit_price), "purchased_at": _iso(purchase.purchased_at),
            "created_at": _iso(purchase.created_at), "label": purchase.label, "note": purchase.note}


def _unexplained_depletion(purchase):
    accounted = TicketConsumption.objects.filter(purchase_id=purchase.id, refunded_at__isnull=True).aggregate(total=Sum("tickets_used"))["total"] or 0
    return int(purchase.total_tickets) - int(purchase.remaining_tickets) - int(accounted)


def _classification(reservation, ledger, candidates, purchases, repair_reason):
    if len(candidates) > 1:
        return "multiple_purchase_candidates"
    if len(candidates) == 1:
        purchase = candidates[0]
        if int(purchase.unit_price or 0) <= 0:
            return "price_evidence_missing"
        expected = int(purchase.unit_price) * int(reservation.tickets_used)
        if int(reservation.participant_ticket_price_snapshot or 0) != expected:
            return "single_but_inconsistent"
        return "other"
    later_matches = [purchase for purchase in purchases if purchase.purchased_at > ledger.created_at and _unexplained_depletion(purchase) == int(reservation.tickets_used)]
    if len(later_matches) == 1:
        return "single_but_inconsistent"
    if purchases and all(purchase.purchase_type == TicketPurchase.PURCHASE_TYPE_LEGACY for purchase in purchases):
        return "legacy_only"
    if repair_reason == "unique_purchase_lot_required":
        return "no_purchase_candidate"
    return "other"


def _timeline(user_id):
    events = []
    for row in TicketPurchase.objects.filter(user_id=user_id):
        events.append({"event_type": "purchase", "occurred_at": _iso(row.purchased_at), **_purchase_row(row)})
    for row in TicketLedger.objects.filter(user_id=user_id):
        events.append({"event_type": "ledger", "occurred_at": _iso(row.created_at), "ledger_id": row.id,
                       "reservation_id": row.reservation_id, "change_amount": int(row.change_amount),
                       "balance_after": int(row.balance_after), "reason": row.reason, "created_at": _iso(row.created_at)})
    for row in TicketConsumption.objects.filter(user_id=user_id):
        events.append({"event_type": "consumption", "occurred_at": _iso(row.created_at), "consumption_id": row.id,
                       "reservation_id": row.reservation_id, "purchase_id": row.purchase_id,
                       "tickets_used": int(row.tickets_used), "unit_price_snapshot": int(row.unit_price_snapshot),
                       "refunded_at": _iso(row.refunded_at), "created_at": _iso(row.created_at)})
    return sorted(events, key=lambda row: (row["occurred_at"] or "", row["event_type"], row.get("ledger_id", row.get("purchase_id", row.get("consumption_id", 0)))))


def audit_missing_ticket_purchase_evidence(reservation_ids=DEFAULT_RESERVATION_IDS):
    rows, user_ids = [], set()
    for reservation_id in reservation_ids:
        reservation = Reservation.objects.select_related("user").get(pk=reservation_id)
        preview = inspect_legacy_ticket_consumption_repair(reservation_id)
        ledgers = list(TicketLedger.objects.filter(reservation_id=reservation_id, user_id=reservation.user_id,
            reason=TicketLedger.REASON_RESERVATION_USE, change_amount=-int(reservation.tickets_used)).order_by("id"))
        ledger = ledgers[0] if len(ledgers) == 1 else None
        purchases = list(TicketPurchase.objects.filter(user_id=reservation.user_id).order_by("purchased_at", "id"))
        candidates = candidate_purchases(reservation, ledger) if ledger else []
        classification = _classification(reservation, ledger, candidates, purchases, preview.reason) if ledger else "other"
        user_ids.add(reservation.user_id)
        rows.append({"reservation_id": reservation.id, "user_id": reservation.user_id,
            "participant_name": preview.participant_name, "lesson_date": _iso(reservation.start_at),
            "ticket_consumed_at": _iso(reservation.ticket_consumed_at),
            "current_ticket_balance": int(reservation.user.ticket_balance or 0),
            "reservation_ledger": None if ledger is None else {"ledger_id": ledger.id,
                "change_amount": int(ledger.change_amount), "balance_after": int(ledger.balance_after),
                "reason": ledger.reason, "created_at": _iso(ledger.created_at)},
            "candidate_purchases": [_purchase_row(row) for row in candidates], "candidate_count": len(candidates),
            "candidate_classification": classification, "repair_rejection_reason": preview.reason})
    names = ("no_purchase_candidate", "multiple_purchase_candidates", "single_but_inconsistent", "price_evidence_missing", "legacy_only", "other")
    counts = {name: sum(row["candidate_classification"] == name for row in rows) for name in names}
    balances = {row.id: int(row.ticket_balance or 0) for row in User.objects.filter(id__in=user_ids)}
    summary = {"missing_count": len(rows), "no_purchase_candidate_count": counts["no_purchase_candidate"],
        "multiple_purchase_candidate_count": counts["multiple_purchase_candidates"],
        "single_but_inconsistent_count": counts["single_but_inconsistent"],
        "price_evidence_missing_count": counts["price_evidence_missing"], "legacy_only_count": counts["legacy_only"],
        "other_count": counts["other"], "negative_balance_user_count": sum(value < 0 for value in balances.values()),
        "zero_balance_user_count": sum(value == 0 for value in balances.values()),
        "positive_balance_user_count": sum(value > 0 for value in balances.values())}
    return {"read_only": True, "rows": rows,
            "user_timelines": {str(user_id): _timeline(user_id) for user_id in sorted(user_ids)}, "summary": summary}
