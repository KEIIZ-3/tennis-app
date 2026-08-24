"""Read-only diagnostics for persisted ticket accounting relationships."""

from collections import defaultdict

from .lesson_participants import CANCELED_RESERVATION_STATUSES
from .models import (
    Reservation,
    ReservationParticipant,
    TicketConsumption,
    TicketBurdenChange,
    TicketLedger,
    TicketPurchase,
    User,
    is_preopen_cash_lesson_date,
)


def _finding(reason, **values):
    return {**values, "reason": reason}


def diagnose_ticket_integrity():
    """Return deterministic, PII-free diagnostics without writing any model."""
    users = list(User.objects.order_by("id").values("id", "ticket_balance"))
    purchases = list(
        TicketPurchase.objects.order_by("id").values(
            "id", "user_id", "purchase_type", "total_tickets",
            "remaining_tickets", "unit_price",
        )
    )
    consumptions = list(
        TicketConsumption.objects.order_by("id").values(
            "id", "user_id", "purchase_id", "reservation_id", "fixed_lesson_id",
            "tickets_used", "unit_price_snapshot", "refunded_at",
        )
    )
    ledgers = list(
        TicketLedger.objects.order_by("user_id", "created_at", "id").values(
            "id", "user_id", "reservation_id", "fixed_lesson_id",
            "change_amount", "balance_after", "reason",
        )
    )
    reservations = list(
        Reservation.objects.order_by("id").values(
            "id", "user_id", "fixed_lesson_id", "is_fixed_entry", "lesson_type",
            "start_at", "tickets_used", "ticket_consumed_at", "ticket_refunded_at",
            "status", "payment_status",
        )
    )
    family_ids = set(
        ReservationParticipant.objects.filter(participant_type="family")
        .values_list("reservation_id", flat=True)
    )
    formal_cross_payers = set(
        TicketBurdenChange.objects.values_list("reservation_id", "new_payer_id")
    )

    purchase_by_id = {row["id"]: row for row in purchases}
    reservation_by_id = {row["id"]: row for row in reservations}
    purchase_remaining = defaultdict(int)
    legacy_users = set()
    purchase_findings = []
    for row in purchases:
        purchase_remaining[row["user_id"]] += int(row["remaining_tickets"])
        if row["purchase_type"] == TicketPurchase.PURCHASE_TYPE_LEGACY:
            legacy_users.add(row["user_id"])
        if row["remaining_tickets"] < 0:
            purchase_findings.append(_finding("negative_remaining_tickets", purchase_id=row["id"], remaining_tickets=row["remaining_tickets"]))
        if row["total_tickets"] < 0:
            purchase_findings.append(_finding("negative_total_tickets", purchase_id=row["id"], total_tickets=row["total_tickets"]))
        if row["remaining_tickets"] > row["total_tickets"]:
            purchase_findings.append(_finding("remaining_exceeds_total", purchase_id=row["id"], remaining_tickets=row["remaining_tickets"], total_tickets=row["total_tickets"]))
        if row["unit_price"] < 0:
            purchase_findings.append(_finding("negative_unit_price", purchase_id=row["id"], unit_price=row["unit_price"]))

    balance_findings = []
    balance_unverifiable = []
    for user in users:
        expected = purchase_remaining[user["id"]]
        actual = int(user["ticket_balance"])
        if expected == actual:
            continue
        detail = {"user_id": user["id"], "expected_balance": expected, "actual_balance": actual}
        if user["id"] in legacy_users:
            balance_findings.append(_finding("balance_purchase_remaining_mismatch", **detail))
        else:
            balance_unverifiable.append(_finding("no_legacy_baseline", **detail))

    consumption_findings = []
    consumption_special = {"fixed_lesson": 0, "legacy_purchase": 0, "zero_price": 0, "pending_purchase": 0}
    by_reservation = defaultdict(list)
    for row in consumptions:
        purchase = purchase_by_id.get(row["purchase_id"])
        reservation = reservation_by_id.get(row["reservation_id"])
        if purchase and row["user_id"] != purchase["user_id"]:
            consumption_findings.append(_finding("purchase_user_mismatch", consumption_id=row["id"], purchase_id=row["purchase_id"], user_id=row["user_id"]))
        if (
            reservation
            and row["user_id"] != reservation["user_id"]
            and (row["reservation_id"], row["user_id"]) not in formal_cross_payers
        ):
            consumption_findings.append(_finding("reservation_user_mismatch", consumption_id=row["id"], reservation_id=row["reservation_id"], user_id=row["user_id"]))
        if not row["reservation_id"] and not row["fixed_lesson_id"]:
            consumption_findings.append(_finding("missing_consumption_target", consumption_id=row["id"]))
        if row["reservation_id"] and row["fixed_lesson_id"] and reservation and reservation["fixed_lesson_id"] != row["fixed_lesson_id"]:
            consumption_findings.append(_finding("fixed_lesson_reference_mismatch", consumption_id=row["id"], reservation_id=row["reservation_id"]))
        if int(row["tickets_used"]) <= 0:
            consumption_findings.append(_finding("nonpositive_tickets_used", consumption_id=row["id"], tickets_used=row["tickets_used"]))
        if row["fixed_lesson_id"]:
            consumption_special["fixed_lesson"] += 1
        if purchase and purchase["purchase_type"] == TicketPurchase.PURCHASE_TYPE_LEGACY:
            consumption_special["legacy_purchase"] += 1
        if not purchase:
            consumption_special["pending_purchase"] += 1
        if int(row["unit_price_snapshot"] or 0) == 0:
            consumption_special["zero_price"] += 1
        if row["reservation_id"]:
            by_reservation[row["reservation_id"]].append(row)

    reservation_findings = []
    reservation_unverifiable = []
    reservation_special = {"family": 0, "fixed_lesson": 0, "preopen": 0, "waived": 0, "zero_ticket": 0}
    canceled_statuses = set(CANCELED_RESERVATION_STATUSES)
    for row in reservations:
        rows = by_reservation[row["id"]]
        if row["id"] in family_ids:
            reservation_special["family"] += 1
        if row["is_fixed_entry"] or row["fixed_lesson_id"]:
            reservation_special["fixed_lesson"] += 1
        if row["lesson_type"] == Reservation.LESSON_GENERAL and is_preopen_cash_lesson_date(row["start_at"]):
            reservation_special["preopen"] += 1
            continue
        if row["payment_status"] == Reservation.PAYMENT_STATUS_WAIVED:
            reservation_special["waived"] += 1
            continue
        if int(row["tickets_used"]) == 0:
            reservation_special["zero_ticket"] += 1
            continue
        active_rows = [item for item in rows if item["refunded_at"] is None]
        total = sum(int(item["tickets_used"]) for item in active_rows)
        is_canceled = row["status"] in canceled_statuses
        if is_canceled and active_rows:
            reservation_findings.append(_finding("canceled_with_unrefunded_consumption", reservation_id=row["id"], tickets_used=sum(int(item["tickets_used"]) for item in active_rows)))
        if not is_canceled and rows and not active_rows:
            reservation_findings.append(_finding("active_with_all_consumptions_refunded", reservation_id=row["id"]))
        if rows and not row["ticket_consumed_at"]:
            # Historical consumptions are persisted accounting evidence even when
            # the later Reservation marker is absent.  Do not call this damage or
            # suggest recreating/refunding an otherwise valid consumption.
            reservation_unverifiable.append(_finding(
                "historical_consumption_without_consumed_at_marker",
                reservation_id=row["id"],
            ))
        if row["ticket_refunded_at"] and active_rows:
            reservation_findings.append(_finding("refunded_at_with_unrefunded_consumption", reservation_id=row["id"]))
        if not is_canceled and rows and total != int(row["tickets_used"]):
            reservation_findings.append(_finding("reservation_consumption_ticket_mismatch", reservation_id=row["id"], expected_tickets=row["tickets_used"], actual_tickets=total))
        if row["ticket_consumed_at"] and not rows:
            reservation_unverifiable.append(_finding("consumed_at_without_consumption_evidence", reservation_id=row["id"], tickets_used=row["tickets_used"]))
        if row["ticket_refunded_at"] and rows and not all(item["refunded_at"] for item in rows):
            pass  # already a proven finding above
        elif is_canceled and rows and all(item["refunded_at"] for item in rows) and not row["ticket_refunded_at"]:
            reservation_findings.append(_finding("refunded_consumptions_without_refunded_at", reservation_id=row["id"]))

    ledger_findings = []
    ledger_unverifiable = []
    ledger_by_user = defaultdict(list)
    for row in ledgers:
        ledger_by_user[row["user_id"]].append(row)
    user_balance = {row["id"]: int(row["ticket_balance"]) for row in users}
    for user_id in sorted(user_balance):
        rows = ledger_by_user[user_id]
        if not rows:
            ledger_unverifiable.append({"user_id": user_id, "reason": "no_ledger_baseline"})
            continue
        previous = rows[0]
        for row in rows[1:]:
            expected = int(previous["balance_after"]) + int(row["change_amount"])
            if int(row["balance_after"]) != expected:
                ledger_findings.append(_finding("ledger_balance_chain_mismatch", ledger_id=row["id"], user_id=user_id, expected_balance=expected, actual_balance=row["balance_after"]))
            previous = row
        if int(rows[-1]["balance_after"]) != user_balance[user_id]:
            ledger_findings.append(_finding("latest_ledger_balance_mismatch", ledger_id=rows[-1]["id"], user_id=user_id, expected_balance=rows[-1]["balance_after"], actual_balance=user_balance[user_id]))

    balance_findings.sort(key=lambda item: (item["user_id"], item["reason"]))
    purchase_findings.sort(key=lambda item: (item["purchase_id"], item["reason"]))
    consumption_findings.sort(key=lambda item: (item["consumption_id"], item["reason"]))
    reservation_findings.sort(key=lambda item: (item["reservation_id"], item["reason"]))
    ledger_findings.sort(key=lambda item: (item["ledger_id"], item["reason"]))
    proven = balance_findings + purchase_findings + consumption_findings + reservation_findings + ledger_findings
    return {
        "user_balance_summary": {"user_count": len(users), "matched": len(users) - len(balance_findings) - len(balance_unverifiable), "finding": len(balance_findings), "unverifiable": len(balance_unverifiable)},
        "purchase_summary": {"purchase_count": len(purchases), "legacy_purchase_count": sum(1 for row in purchases if row["purchase_type"] == TicketPurchase.PURCHASE_TYPE_LEGACY), "finding": len(purchase_findings)},
        "consumption_summary": {"consumption_count": len(consumptions), "refunded_count": sum(1 for row in consumptions if row["refunded_at"]), "finding": len(consumption_findings)},
        "ledger_summary": {"ledger_count": len(ledgers), "finding": len(ledger_findings), "unverifiable_user_count": len(ledger_unverifiable)},
        "reservation_ticket_summary": {"reservation_count": len(reservations), "finding": len(reservation_findings), "unverifiable": len(reservation_unverifiable)},
        "balance_findings": balance_findings,
        "purchase_findings": purchase_findings,
        "consumption_findings": consumption_findings,
        "ledger_findings": ledger_findings,
        "reservation_findings": reservation_findings,
        "unverifiable": {"balance": balance_unverifiable, "ledger": ledger_unverifiable, "reservation": reservation_unverifiable},
        "special_cases": {"consumption": consumption_special, "reservation": reservation_special},
        "finding_count": len(proven),
    }
