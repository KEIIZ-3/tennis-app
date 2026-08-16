"""Read-only, PII-free classification of legacy ticket accounting evidence."""

from collections import defaultdict

from .lesson_participants import CANCELED_RESERVATION_STATUSES
from .models import (
    Reservation,
    ReservationParticipant,
    TicketConsumption,
    TicketLedger,
    TicketPurchase,
    User,
    is_preopen_cash_lesson_date,
)


CONSUMPTION_REASONS = {
    TicketLedger.REASON_RESERVATION_USE,
    TicketLedger.REASON_FIXED_USE,
}
REFUND_REASONS = {
    TicketLedger.REASON_CANCEL_REFUND,
    TicketLedger.REASON_RAIN_REFUND,
}
CANCELED_STATUSES = set(CANCELED_RESERVATION_STATUSES)


def _ids(rows):
    return [row["id"] for row in rows]


def diagnose_legacy_ticket_evidence():
    """Classify persisted evidence only; never infer or mutate accounting history."""
    users = list(User.objects.order_by("id").values("id", "ticket_balance"))
    purchases = list(
        TicketPurchase.objects.order_by("id").values(
            "id", "user_id", "purchase_type", "total_tickets", "remaining_tickets"
        )
    )
    consumptions = list(
        TicketConsumption.objects.order_by("id").values(
            "id", "user_id", "purchase_id", "reservation_id", "fixed_lesson_id",
            "tickets_used", "refunded_at",
        )
    )
    ledgers = list(
        TicketLedger.objects.order_by("user_id", "created_at", "id").values(
            "id", "user_id", "reservation_id", "fixed_lesson_id", "change_amount",
            "balance_after", "reason",
        )
    )
    reservations = list(
        Reservation.objects.order_by("id").values(
            "id", "user_id", "fixed_lesson_id", "is_fixed_entry", "lesson_type",
            "start_at", "tickets_used", "ticket_consumed_at", "ticket_refunded_at",
            "status",
        )
    )
    family_ids = set(
        ReservationParticipant.objects.filter(participant_type="family")
        .values_list("reservation_id", flat=True)
    )

    purchases_by_user = defaultdict(list)
    for row in purchases:
        purchases_by_user[row["user_id"]].append(row)
    consumptions_by_user = defaultdict(list)
    consumptions_by_reservation = defaultdict(list)
    for row in consumptions:
        consumptions_by_user[row["user_id"]].append(row)
        if row["reservation_id"]:
            consumptions_by_reservation[row["reservation_id"]].append(row)
    ledgers_by_user = defaultdict(list)
    ledgers_by_reservation = defaultdict(list)
    for row in ledgers:
        ledgers_by_user[row["user_id"]].append(row)
        if row["reservation_id"]:
            ledgers_by_reservation[row["reservation_id"]].append(row)
    reservations_by_user = defaultdict(list)
    for row in reservations:
        reservations_by_user[row["user_id"]].append(row)

    reservation_rows = []
    for reservation in reservations:
        if not reservation["ticket_consumed_at"] or int(reservation["tickets_used"]) <= 0:
            continue
        if consumptions_by_reservation[reservation["id"]]:
            continue
        related_ledgers = ledgers_by_reservation[reservation["id"]]
        exact_consumption_ledgers = [
            row for row in related_ledgers
            if row["reason"] in CONSUMPTION_REASONS
            and int(row["change_amount"]) == -int(reservation["tickets_used"])
        ]
        exact_refund_ledgers = [
            row for row in related_ledgers
            if row["reason"] in REFUND_REASONS
            and int(row["change_amount"]) == int(reservation["tickets_used"])
        ]
        is_fixed = bool(reservation["is_fixed_entry"] and reservation["fixed_lesson_id"])
        has_fixed_reference = bool(reservation["fixed_lesson_id"])
        is_preopen = (
            reservation["lesson_type"] == Reservation.LESSON_GENERAL
            and is_preopen_cash_lesson_date(reservation["start_at"])
        )
        user_purchases = purchases_by_user[reservation["user_id"]]
        legacy_purchase_ids = [
            row["id"] for row in user_purchases
            if row["purchase_type"] == TicketPurchase.PURCHASE_TYPE_LEGACY
        ]
        evidence = []
        if is_fixed:
            evidence.append("fixed_entry_with_fixed_lesson_reference")
        elif has_fixed_reference:
            evidence.append("fixed_lesson_reference_only")
        if is_preopen:
            evidence.append("persisted_preopen_lesson_date_and_type")
        if exact_consumption_ledgers:
            evidence.append("exact_reservation_consumption_ledger")
        if exact_refund_ledgers:
            evidence.append("exact_reservation_refund_ledger")
        if reservation["status"] in CANCELED_STATUSES:
            evidence.append("persisted_canceled_status")
        if reservation["ticket_refunded_at"]:
            evidence.append("persisted_ticket_refunded_at")
        if legacy_purchase_ids:
            evidence.append("user_has_legacy_purchase_lot")
        if reservation["id"] in family_ids:
            evidence.append("family_participant_snapshot_exists")

        if is_preopen:
            classification = "preopen_with_legacy_ticket_markers"
        elif is_fixed:
            classification = "confirmed_legacy_fixed_lesson_shape"
        elif exact_consumption_ledgers:
            classification = "consumption_proven_by_exact_ledger"
        elif legacy_purchase_ids:
            classification = "legacy_operation_supported_not_reservation_proven"
        else:
            classification = "indeterminate_insufficient_persisted_evidence"
        recoverable = bool(exact_consumption_ledgers)
        reservation_rows.append({
            "reservation_id": reservation["id"],
            "user_id": reservation["user_id"],
            "classification": classification,
            "recoverability": "recoverable_accounting_event" if recoverable else "indeterminate",
            "evidence": evidence,
            "tickets_used": int(reservation["tickets_used"]),
            "consumption_ledger_ids": _ids(exact_consumption_ledgers),
            "refund_ledger_ids": _ids(exact_refund_ledgers),
            "legacy_purchase_ids": legacy_purchase_ids,
            "fixed_lesson_id": reservation["fixed_lesson_id"],
        })

    balance_rows = []
    ledger_baseline_rows = []
    for user in users:
        user_id = user["id"]
        user_purchases = purchases_by_user[user_id]
        user_consumptions = consumptions_by_user[user_id]
        user_ledgers = ledgers_by_user[user_id]
        purchase_remaining = sum(int(row["remaining_tickets"]) for row in user_purchases)
        actual = int(user["ticket_balance"])
        legacy_purchases = [row for row in user_purchases if row["purchase_type"] == TicketPurchase.PURCHASE_TYPE_LEGACY]
        if actual != purchase_remaining and not legacy_purchases:
            evidence = []
            if actual < 0:
                evidence.append("negative_current_balance")
            if not user_purchases:
                evidence.append("no_purchase_history")
            if not user_ledgers:
                evidence.append("no_ledger_history")
            if user_consumptions:
                evidence.append("consumption_history_exists")
            fixed_consumptions = [row for row in user_consumptions if row["fixed_lesson_id"]]
            if fixed_consumptions:
                evidence.append("fixed_lesson_consumption_exists")
            required_opening_balance = actual - purchase_remaining
            ledger_chain_consistent = all(
                int(current["balance_after"]) == int(previous["balance_after"]) + int(current["change_amount"])
                for previous, current in zip(user_ledgers, user_ledgers[1:])
            )
            latest_ledger_matches = bool(user_ledgers) and int(user_ledgers[-1]["balance_after"]) == actual
            if ledger_chain_consistent and latest_ledger_matches:
                evidence.append("current_ledger_tail_matches_balance")
            balance_rows.append({
                "user_id": user_id,
                "classification": (
                    "negative_balance"
                    if actual < 0 else
                    "opening_balance_required_but_unproven"
                ),
                "recoverability": "unrecoverable_from_current_persisted_history",
                "current_balance": actual,
                "purchase_remaining_total": purchase_remaining,
                "required_unproven_opening_balance": required_opening_balance,
                "ledger_chain_consistent_from_first_persisted_row": ledger_chain_consistent,
                "latest_ledger_matches_current_balance": latest_ledger_matches,
                "purchase_ids": _ids(user_purchases),
                "ledger_ids": _ids(user_ledgers),
                "consumption_ids": _ids(user_consumptions),
                "evidence": evidence,
            })

        if not user_ledgers:
            evidence = []
            if not user_purchases and not user_consumptions and not reservations_by_user[user_id]:
                classification = "no_ticket_activity_evidence"
            elif user_consumptions:
                classification = "consumption_without_ledger_requires_investigation"
                evidence.append("consumption_history_exists")
            elif any(row["purchase_type"] != TicketPurchase.PURCHASE_TYPE_LEGACY for row in user_purchases):
                classification = "nonlegacy_purchase_without_ledger_requires_investigation"
                evidence.append("purchase_history_exists")
            elif user_purchases:
                classification = "legacy_purchase_without_ledger_baseline"
                evidence.append("purchase_history_exists")
            else:
                classification = "reservation_or_balance_only_without_any_ledger"
            if legacy_purchases:
                evidence.append("legacy_purchase_lot_exists")
            if any(row["fixed_lesson_id"] for row in user_consumptions):
                evidence.append("fixed_lesson_consumption_exists")
            ledger_baseline_rows.append({
                "user_id": user_id,
                "classification": classification,
                "recoverability": "indeterminate_no_ledger_baseline",
                "purchase_ids": _ids(user_purchases),
                "consumption_ids": _ids(user_consumptions),
                "reservation_ids": _ids(reservations_by_user[user_id]),
                "evidence": evidence,
            })

    def counts(rows):
        result = defaultdict(int)
        for row in rows:
            result[row["classification"]] += 1
        return dict(sorted(result.items()))

    return {
        "schema_version": 1,
        "read_only": True,
        "recoverability_policy": "recoverable only when an exact persisted reservation ledger proves the accounting event; no price or time inference",
        "reservation_evidence": {"count": len(reservation_rows), "classification_counts": counts(reservation_rows), "rows": reservation_rows},
        "balance_evidence": {"count": len(balance_rows), "classification_counts": counts(balance_rows), "rows": balance_rows},
        "ledger_baseline_evidence": {"count": len(ledger_baseline_rows), "classification_counts": counts(ledger_baseline_rows), "rows": ledger_baseline_rows},
    }
