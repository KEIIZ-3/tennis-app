"""Deterministic, read-only evidence of persisted ticket state."""

import hashlib
import json
from collections import defaultdict

from .models import Reservation, TicketConsumption, TicketLedger, TicketPurchase, User


SCHEMA_VERSION = 1


def _isoformat(value):
    return value.isoformat() if value is not None else None


def _canonical_json(payload):
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def build_ticket_state_snapshot():
    """Return PII-free persisted ticket state without modifying the database."""
    users = list(User.objects.order_by("id").values("id", "ticket_balance"))
    purchases = list(
        TicketPurchase.objects.order_by("id").values(
            "id",
            "user_id",
            "purchase_type",
            "total_tickets",
            "remaining_tickets",
            "unit_price",
            "purchased_at",
            "created_at",
        )
    )
    consumptions = list(
        TicketConsumption.objects.order_by("id").values(
            "id",
            "user_id",
            "purchase_id",
            "reservation_id",
            "fixed_lesson_id",
            "tickets_used",
            "unit_price_snapshot",
            "refunded_at",
            "created_at",
        )
    )
    ledgers = list(
        TicketLedger.objects.order_by("id").values(
            "id",
            "user_id",
            "change_amount",
            "balance_after",
            "reason",
            "reservation_id",
            "fixed_lesson_id",
            "created_at",
        )
    )
    reservations = list(
        Reservation.objects.order_by("id").values(
            "id",
            "user_id",
            "status",
            "tickets_used",
            "ticket_consumed_at",
            "ticket_refunded_at",
            "participant_ticket_price_snapshot",
        )
    )

    purchase_totals = defaultdict(int)
    active_purchase_counts = defaultdict(int)
    consumption_counts = defaultdict(int)
    refunded_consumption_counts = defaultdict(int)
    ledger_counts = defaultdict(int)
    for row in purchases:
        purchase_totals[row["user_id"]] += int(row["remaining_tickets"])
        if int(row["remaining_tickets"]) > 0:
            active_purchase_counts[row["user_id"]] += 1
    for row in consumptions:
        consumption_counts[row["user_id"]] += 1
        if row["refunded_at"] is not None:
            refunded_consumption_counts[row["user_id"]] += 1
    for row in ledgers:
        ledger_counts[row["user_id"]] += 1

    payload = {
        "schema_version": SCHEMA_VERSION,
        "read_only": True,
        "counts": {
            "users": len(users),
            "purchases": len(purchases),
            "consumptions": len(consumptions),
            "ledgers": len(ledgers),
            "reservations": len(reservations),
        },
        "users": [
            {
                "user_id": row["id"],
                "persisted": {"ticket_balance": int(row["ticket_balance"])},
                "derived": {
                    "purchase_remaining_total": purchase_totals[row["id"]],
                    "active_purchase_count": active_purchase_counts[row["id"]],
                    "consumption_count": consumption_counts[row["id"]],
                    "refunded_consumption_count": refunded_consumption_counts[row["id"]],
                    "ledger_count": ledger_counts[row["id"]],
                },
            }
            for row in users
        ],
        "purchases": [
            {
                "purchase_id": row["id"],
                "user_id": row["user_id"],
                "purchase_type": row["purchase_type"],
                "total_tickets": int(row["total_tickets"]),
                "remaining_tickets": int(row["remaining_tickets"]),
                "unit_price": int(row["unit_price"]),
                "purchased_at": _isoformat(row["purchased_at"]),
                "created_at": _isoformat(row["created_at"]),
            }
            for row in purchases
        ],
        "consumptions": [
            {
                "consumption_id": row["id"],
                "user_id": row["user_id"],
                "purchase_id": row["purchase_id"],
                "reservation_id": row["reservation_id"],
                "fixed_lesson_id": row["fixed_lesson_id"],
                "tickets_used": int(row["tickets_used"]),
                "unit_price_snapshot": int(row["unit_price_snapshot"]),
                "refunded_at": _isoformat(row["refunded_at"]),
                "created_at": _isoformat(row["created_at"]),
            }
            for row in consumptions
        ],
        "ledgers": [
            {
                "ledger_id": row["id"],
                "user_id": row["user_id"],
                "change_amount": int(row["change_amount"]),
                "balance_after": int(row["balance_after"]),
                "reason": row["reason"],
                "reservation_id": row["reservation_id"],
                "fixed_lesson_id": row["fixed_lesson_id"],
                "created_at": _isoformat(row["created_at"]),
            }
            for row in ledgers
        ],
        "reservations": [
            {
                "reservation_id": row["id"],
                "user_id": row["user_id"],
                "status": row["status"],
                "tickets_used": int(row["tickets_used"]),
                "ticket_consumed_at": _isoformat(row["ticket_consumed_at"]),
                "ticket_refunded_at": _isoformat(row["ticket_refunded_at"]),
                "participant_ticket_price_snapshot": row["participant_ticket_price_snapshot"],
            }
            for row in reservations
        ],
    }
    return {
        **payload,
        "fingerprint": {
            "algorithm": "sha256",
            "canonicalization": "json-sort-keys-compact-utf8-v1",
            "value": hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest(),
        },
    }


def serialize_ticket_state_snapshot(snapshot):
    """Serialize a snapshot with a stable key order and no volatile whitespace."""
    return _canonical_json(snapshot)
