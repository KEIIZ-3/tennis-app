"""Read-only plan for legacy ticket repairs supported by persisted evidence."""

from collections import Counter, defaultdict

from .legacy_ticket_evidence_diagnostic import (
    CANCELED_STATUSES,
    CONSUMPTION_REASONS,
    REFUND_REASONS,
    diagnose_legacy_ticket_evidence,
)
from .models import Reservation, TicketConsumption, TicketLedger, TicketPurchase, User


A = "fully_repairable_a"
B = "partial_evidence_b"
C = "repair_forbidden_c"
NON_TARGET = "non_repair_target"


def _counts(rows):
    return dict(sorted(Counter(row["classification"] for row in rows).items()))


def diagnose_legacy_ticket_repair_plan():
    """Return a deterministic, PII-free plan; this function performs no writes."""
    evidence = diagnose_legacy_ticket_evidence()
    users = {row["id"]: row for row in User.objects.order_by("id").values("id", "ticket_balance")}
    purchases = list(TicketPurchase.objects.order_by("purchased_at", "id").values(
        "id", "user_id", "purchase_type", "total_tickets", "remaining_tickets",
        "unit_price", "purchased_at",
    ))
    consumptions = list(TicketConsumption.objects.order_by("id").values("id", "user_id"))
    ledgers = list(TicketLedger.objects.order_by("created_at", "id").values(
        "id", "user_id", "reservation_id", "change_amount", "balance_after",
        "reason", "created_at",
    ))
    reservations = {row["id"]: row for row in Reservation.objects.order_by("id").values(
        "id", "user_id", "fixed_lesson_id", "status", "tickets_used",
        "ticket_refunded_at", "participant_ticket_price_snapshot",
    )}
    purchases_by_user = defaultdict(list)
    ledgers_by_user = defaultdict(list)
    ledgers_by_reservation = defaultdict(list)
    consumption_ids_by_user = defaultdict(list)
    for row in purchases:
        purchases_by_user[row["user_id"]].append(row)
    for row in ledgers:
        ledgers_by_user[row["user_id"]].append(row)
        if row["reservation_id"]:
            ledgers_by_reservation[row["reservation_id"]].append(row)
    for row in consumptions:
        consumption_ids_by_user[row["user_id"]].append(row["id"])

    reservation_rows = []
    for source in evidence["reservation_evidence"]["rows"]:
        reservation = reservations[source["reservation_id"]]
        related = ledgers_by_reservation[reservation["id"]]
        consumes = [row for row in related if row["reason"] in CONSUMPTION_REASONS and int(row["change_amount"]) == -int(reservation["tickets_used"])]
        refunds = [row for row in related if row["reason"] in REFUND_REASONS and int(row["change_amount"]) == int(reservation["tickets_used"])]
        consume_at = consumes[0]["created_at"] if len(consumes) == 1 else None
        candidates = [row for row in purchases_by_user[reservation["user_id"]] if consume_at and row["purchased_at"] <= consume_at]
        missing = []
        if len(consumes) != 1:
            missing.append("single_exact_consumption_ledger")
        if len(candidates) != 1:
            missing.append("unique_purchase_lot")
        purchase = candidates[0] if len(candidates) == 1 else None
        if not purchase or int(purchase["unit_price"]) <= 0:
            missing.append("unit_price_snapshot")
        expected_price = int(purchase["unit_price"]) * int(reservation["tickets_used"]) if purchase else None
        if reservation["participant_ticket_price_snapshot"] != expected_price:
            missing.append("participant_price_snapshot_consistency")
        canceled = reservation["status"] in CANCELED_STATUSES or reservation["ticket_refunded_at"] is not None
        if canceled and len(refunds) != 1:
            missing.append("unique_refund_timestamp")
        if not canceled and refunds:
            missing.append("refund_state")
        user_purchases = purchases_by_user[reservation["user_id"]]
        if sum(int(row["remaining_tickets"]) for row in user_purchases) != int(users[reservation["user_id"]]["ticket_balance"]):
            missing.append("current_purchase_and_balance_state")
        user_ledgers = ledgers_by_user[reservation["user_id"]]
        if not user_ledgers or int(user_ledgers[-1]["balance_after"]) != int(users[reservation["user_id"]]["ticket_balance"]):
            missing.append("current_ledger_and_balance_state")
        if purchase and int(purchase["total_tickets"]) < int(reservation["tickets_used"]):
            missing.append("purchase_capacity")
        expected_remaining = (
            int(purchase["total_tickets"]) if canceled else
            int(purchase["total_tickets"]) - int(reservation["tickets_used"])
        ) if purchase else None
        if purchase and int(purchase["remaining_tickets"]) != expected_remaining:
            missing.append("historical_lot_availability")
        other_ticket_events = [
            row for row in user_ledgers
            if row["reason"] in CONSUMPTION_REASONS | REFUND_REASONS
            and row["reservation_id"] != reservation["id"]
        ]
        if consumption_ids_by_user[reservation["user_id"]] or other_ticket_events:
            missing.append("exclusive_lot_history")

        if not consumes:
            classification = C
            forbidden = "no_accounting_event_evidence"
        elif missing:
            classification = B
            forbidden = None
        else:
            classification = A
            forbidden = None
        refund_at = refunds[0]["created_at"] if canceled and len(refunds) == 1 else None
        payload = None
        if classification == A:
            payload = {
                "reservation_id": reservation["id"], "user_id": reservation["user_id"],
                "consumptions": [{"purchase_id": purchase["id"], "tickets_used": int(reservation["tickets_used"]), "unit_price_snapshot": int(purchase["unit_price"]), "refunded_at": refund_at.isoformat() if refund_at else None, "fixed_lesson_id": reservation["fixed_lesson_id"]}],
                "balance_change_required": False, "purchase_remaining_change_required": False,
                "ledger_change_required": False, "participant_snapshot_change_required": False,
            }
        reservation_rows.append({
            "reservation_id": reservation["id"], "user_id": reservation["user_id"],
            "classification": classification, "evidence": sorted(source["evidence"]),
            "missing_required_evidence": sorted(set(missing)),
            "candidate_purchase_ids": [row["id"] for row in candidates],
            "consumption_ledger_ids": [row["id"] for row in consumes],
            "refund_ledger_ids": [row["id"] for row in refunds],
            "repair_forbidden_reason": forbidden, "repair_payload": payload,
        })

    balance_rows = [{**row, "classification": C, "repair_forbidden_reason": "opening_balance_not_persisted", "repair_payload": None} for row in evidence["balance_evidence"]["rows"]]
    baseline_rows = []
    for row in evidence["ledger_baseline_evidence"]["rows"]:
        no_activity = row["classification"] == "no_ticket_activity_evidence"
        baseline_rows.append({**row, "classification": NON_TARGET if no_activity else C, "repair_forbidden_reason": "no_accounting_event_evidence" if no_activity else "ledger_baseline_not_persisted", "repair_payload": None})
    all_rows = reservation_rows + balance_rows + baseline_rows
    totals = Counter(row["classification"] for row in all_rows)
    return {
        "schema_version": 1, "read_only": True,
        "summary": {"fully_repairable_a": totals[A], "partial_evidence_b": totals[B], "repair_forbidden_c": totals[C], "non_repair_target": totals[NON_TARGET]},
        "reservation_plan": {"count": len(reservation_rows), "classification_counts": _counts(reservation_rows), "rows": reservation_rows},
        "balance_plan": {"count": len(balance_rows), "classification_counts": _counts(balance_rows), "rows": balance_rows},
        "ledger_baseline_plan": {"count": len(baseline_rows), "classification_counts": _counts(baseline_rows), "rows": baseline_rows},
    }
