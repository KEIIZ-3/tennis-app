"""Read-only Phase 1 ticket lifecycle audit.

The output is PII-free. Idempotency keys are reduced to prefix counts and are
never emitted. No repair inference is made from unknown legacy opening stock.
"""

import hashlib
import json
from collections import Counter, defaultdict

from django.db import connection
from django.db.models import Count

from .legacy_ticket_evidence_diagnostic import diagnose_legacy_ticket_evidence
from .legacy_ticket_repair_plan import diagnose_legacy_ticket_repair_plan
from .models import Reservation, TicketConsumption, TicketLedger, TicketPurchase, User
from .participant_price_integrity_diagnostic import diagnose_participant_price_integrity
from .ticket_integrity_diagnostic import diagnose_ticket_integrity
from .ticket_state_snapshot import build_ticket_state_snapshot


LEGACY_BASELINE = "d864f93b28f31bfcda826249c55463901bbe5152d6e76f2df224661cc949f517"
SYNTHETIC_LABEL = "旧データ移行分"


def _canonical_hash(payload):
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _key_prefix(key):
    if not key:
        return None
    return key.split(":", 1)[0] + (":" if ":" in key else "")


def diagnose_ticket_lifecycle(*, pr209_deployed_at=None, baseline=LEGACY_BASELINE):
    """Return persisted Phase 1 evidence using SELECT queries only."""
    legacy_snapshot = build_ticket_state_snapshot()
    users = list(User.objects.order_by("id").values("id", "ticket_balance"))
    purchases = list(TicketPurchase.objects.order_by("id").values(
        "id", "user_id", "purchase_type", "total_tickets", "remaining_tickets",
        "unit_price", "label", "note", "purchased_at", "created_at", "idempotency_key",
    ))
    consumptions = list(TicketConsumption.objects.order_by("id").values(
        "id", "user_id", "purchase_id", "reservation_id", "fixed_lesson_id",
        "tickets_used", "unit_price_snapshot", "refunded_at",
    ))
    ledgers = list(TicketLedger.objects.order_by("id").values(
        "id", "user_id", "reservation_id", "change_amount", "balance_after", "reason",
    ))
    reservations = list(Reservation.objects.order_by("id").values(
        "id", "user_id", "status", "tickets_used", "ticket_consumed_at",
        "ticket_refunded_at", "participant_ticket_price_snapshot",
    ))

    remaining = defaultdict(int)
    for row in purchases:
        remaining[row["user_id"]] += int(row["remaining_tickets"])
    balance_classes = Counter()
    for row in users:
        known = remaining[row["id"]]
        actual = int(row["ticket_balance"])
        if actual < 0:
            balance_classes["negative_balance"] += 1
        elif actual > known:
            balance_classes["unknown_positive_balance"] += 1
        elif actual == known:
            balance_classes["fully_evidenced"] += 1
        else:
            balance_classes["balance_below_known_remaining"] += 1

    synthetic = [row for row in purchases if row["purchase_type"] == TicketPurchase.PURCHASE_TYPE_LEGACY and row["label"] == SYNTHETIC_LABEL and int(row["unit_price"]) == 0]
    post_pr209 = None
    if pr209_deployed_at is not None:
        post_pr209 = [row["id"] for row in synthetic if row["created_at"] >= pr209_deployed_at]

    non_null = [row for row in purchases if row["idempotency_key"]]
    duplicate_groups = list(
        TicketPurchase.objects.exclude(idempotency_key__isnull=True)
        .values("idempotency_key").annotate(count=Count("id")).filter(count__gt=1)
    )
    prefixes = Counter(_key_prefix(row["idempotency_key"]) for row in non_null)
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, TicketPurchase._meta.db_table)
    unique_key_constraint = any(
        value.get("unique") and value.get("columns") == ["idempotency_key"]
        for value in constraints.values()
    )

    current_payload = {
        "schema_version": 2,
        "legacy_snapshot_fingerprint": legacy_snapshot["fingerprint"]["value"],
        "idempotency": [
            {"purchase_id": row["id"], "key_hash": hashlib.sha256(row["idempotency_key"].encode("utf-8")).hexdigest()}
            for row in non_null
        ],
    }
    integrity = diagnose_ticket_integrity()
    evidence = diagnose_legacy_ticket_evidence()
    repair_plan = diagnose_legacy_ticket_repair_plan()
    participant = diagnose_participant_price_integrity()
    return {
        "schema_version": 1,
        "read_only": True,
        "database": {"vendor": connection.vendor, "read_only_transaction_required": connection.vendor == "postgresql"},
        "fingerprint": {
            "legacy_compatible": legacy_snapshot["fingerprint"]["value"],
            "baseline": baseline,
            "baseline_matches": legacy_snapshot["fingerprint"]["value"] == baseline,
            "current_schema": _canonical_hash(current_payload),
            "legacy_payload_includes_idempotency_key": False,
            "current_payload_includes_hashed_non_null_idempotency_keys": True,
        },
        "counts": {"users": len(users), "purchases": len(purchases), "consumptions": len(consumptions), "ledgers": len(ledgers), "reservations": len(reservations)},
        "user_balance_classification": dict(sorted(balance_classes.items())),
        "synthetic_legacy_purchase": {
            "candidate_count": len(synthetic),
            "pr209_deployed_at": pr209_deployed_at.isoformat() if pr209_deployed_at else None,
            "created_on_or_after_pr209_count": len(post_pr209) if post_pr209 is not None else None,
            "created_on_or_after_pr209_purchase_ids": post_pr209,
            "timestamp_boundary_required": pr209_deployed_at is None,
        },
        "idempotency": {
            "null_count": len(purchases) - len(non_null), "non_null_count": len(non_null),
            "prefix_counts": dict(sorted(prefixes.items())), "duplicate_non_null_key_group_count": len(duplicate_groups),
            "unique_constraint_present": unique_key_constraint,
            "purchase_ledger_linkage_limit": "TicketLedger does not persist idempotency_key or purchase_id; exact per-key Purchase/Ledger cardinality is not provable.",
        },
        "integrity": integrity,
        "legacy_evidence_summary": {key: value.get("classification_counts", {}) for key, value in evidence.items() if isinstance(value, dict)},
        "repair_plan_summary": repair_plan["summary"],
        "participant_price": participant,
    }
