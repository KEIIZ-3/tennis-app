"""Read-only diagnostics for Reservation participant ticket price snapshots."""

from collections import Counter

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Prefetch

from .fixed_ticket_consumption_repair import REPAIR_NOTE_PREFIX
from .models import (
    Reservation,
    ReservationParticipant,
    TicketConsumption,
    TicketLedger,
    TicketPurchase,
)


def _empty_result():
    return {
        "reservation_count": 0,
        "reservation_status": {
            "active": 0,
            "canceled": 0,
            "rain_canceled": 0,
            "pending": 0,
        },
        "snapshot_summary": {
            "non_null": 0,
            "null": 0,
            "zero": 0,
            "lte_1000": 0,
            "gt_1000": 0,
        },
        "legacy_classification": {
            "recoverable_a": 0,
            "conditional_b": 0,
            "unrecoverable_c": 0,
        },
        "consumption_summary": {
            "none": 0,
            "single": 0,
            "multiple": 0,
            "same_unit_price_multiple": 0,
            "mixed_unit_price": 0,
            "returned": 0,
            "unreturned": 0,
        },
        "snapshot_verification": {"match": 0, "mismatch": 0, "unverifiable": 0},
        "multiple_lot_reservations": [],
        "special_cases": {
            "zero_ticket_reservations": 0,
            "zero_ticket_snapshot_null": 0,
            "zero_ticket_snapshot_non_null": 0,
            "custom_ticket_price": 0,
            "custom_ticket_price_snapshot_null": 0,
            "preopen": 0,
            "preopen_snapshot_null": 0,
            "waived": 0,
            "waived_snapshot_null": 0,
            "family_reservations": 0,
            "same_account_multiple_reservations": 0,
            "zero_price_consumptions": 0,
            "zero_price_formal_free": 0,
            "zero_price_legacy_auto_generated": 0,
            "zero_price_fixed_repair": 0,
            "zero_price_indeterminate": 0,
        },
        "snapshot_mismatches": [],
        "integrity_findings": [],
        "finding_count": 0,
    }


def _zero_price_kind(consumption, repair_reservation_ids):
    if consumption.reservation_id in repair_reservation_ids:
        return "zero_price_fixed_repair"
    purchase = consumption.purchase
    if (
        purchase.purchase_type == TicketPurchase.PURCHASE_TYPE_LEGACY
        and purchase.label == "旧データ移行分"
        and purchase.note == "既存残高との差分を補完"
    ):
        return "zero_price_legacy_auto_generated"
    # There is no persisted field that proves a zero-price lot was formally free.
    return "zero_price_indeterminate"


def diagnose_participant_price_integrity():
    """Return deterministic, serializable diagnostics without database writes."""
    consumptions = TicketConsumption.objects.select_related("purchase").only(
        "id", "reservation_id", "tickets_used", "unit_price_snapshot", "refunded_at",
        "purchase__id", "purchase__purchase_type", "purchase__label", "purchase__note",
    ).order_by("id")
    reservations = list(
        Reservation.objects.only(
            "id", "user_id", "status", "lesson_type", "start_at", "tickets_used",
            "participant_ticket_price_snapshot", "ticket_consumed_at", "ticket_refunded_at",
            "custom_ticket_price", "payment_status", "payment_amount",
        )
        .prefetch_related(
            Prefetch("ticket_consumptions", queryset=consumptions, to_attr="diagnostic_consumptions"),
            Prefetch(
                "participant_snapshot",
                queryset=ReservationParticipant.objects.only("id", "reservation_id", "participant_type"),
            ),
        )
        .order_by("id")
    )
    result = _empty_result()
    result["reservation_count"] = len(reservations)

    repair_reservation_ids = set(
        TicketLedger.objects.filter(
            reason=TicketLedger.REASON_FIXED_USE,
            note__startswith=REPAIR_NOTE_PREFIX,
        ).values_list("reservation_id", flat=True)
    )
    account_counts = Counter(reservation.user_id for reservation in reservations)

    for reservation in reservations:
        status = reservation.status
        if status in result["reservation_status"]:
            result["reservation_status"][status] += 1

        snapshot = reservation.participant_ticket_price_snapshot
        if snapshot is None:
            result["snapshot_summary"]["null"] += 1
        else:
            result["snapshot_summary"]["non_null"] += 1
            if snapshot == 0:
                result["snapshot_summary"]["zero"] += 1
            if snapshot <= 1000:
                result["snapshot_summary"]["lte_1000"] += 1
            else:
                result["snapshot_summary"]["gt_1000"] += 1

        rows = reservation.diagnostic_consumptions
        row_count = len(rows)
        if row_count == 0:
            result["consumption_summary"]["none"] += 1
        elif row_count == 1:
            result["consumption_summary"]["single"] += 1
        else:
            result["consumption_summary"]["multiple"] += 1
            prices = sorted({int(row.unit_price_snapshot) for row in rows})
            key = "same_unit_price_multiple" if len(prices) == 1 else "mixed_unit_price"
            result["consumption_summary"][key] += 1
            result["multiple_lot_reservations"].append({
                "reservation_id": reservation.pk,
                "consumption_count": row_count,
                "unit_prices": prices,
                "ticket_count": sum(int(row.tickets_used) for row in rows),
                "price_total": sum(int(row.unit_price_snapshot) * int(row.tickets_used) for row in rows),
            })

        if any(row.refunded_at is not None for row in rows):
            result["consumption_summary"]["returned"] += 1
        if any(row.refunded_at is None for row in rows):
            result["consumption_summary"]["unreturned"] += 1

        evidence_tickets = sum(int(row.tickets_used) for row in rows)
        evidence_price = sum(int(row.unit_price_snapshot) * int(row.tickets_used) for row in rows)
        complete_evidence = bool(rows) and evidence_tickets == int(reservation.tickets_used)
        has_zero_price = any(int(row.unit_price_snapshot) == 0 for row in rows)
        is_preopen = reservation.is_preopen_cash_lesson()
        is_special = (
            int(reservation.custom_ticket_price) > 0
            or is_preopen
            or reservation.payment_status == Reservation.PAYMENT_STATUS_WAIVED
            or has_zero_price
        )

        if snapshot is None:
            if int(reservation.tickets_used) <= 0 or not complete_evidence:
                result["legacy_classification"]["unrecoverable_c"] += 1
            elif is_special:
                result["legacy_classification"]["conditional_b"] += 1
            else:
                result["legacy_classification"]["recoverable_a"] += 1
        elif complete_evidence:
            if int(snapshot) == evidence_price:
                result["snapshot_verification"]["match"] += 1
            else:
                result["snapshot_verification"]["mismatch"] += 1
                result["snapshot_mismatches"].append({
                    "reservation_id": reservation.pk,
                    "snapshot": int(snapshot),
                    "evidence_price": evidence_price,
                    "evidence_tickets": evidence_tickets,
                })
        else:
            result["snapshot_verification"]["unverifiable"] += 1
            result["integrity_findings"].append({
                "reservation_id": reservation.pk,
                "reason": "snapshot_without_complete_consumption_evidence",
                "reservation_tickets": int(reservation.tickets_used),
                "evidence_tickets": evidence_tickets,
            })

        if int(reservation.tickets_used) == 0:
            result["special_cases"]["zero_ticket_reservations"] += 1
            suffix = "null" if snapshot is None else "non_null"
            result["special_cases"][f"zero_ticket_snapshot_{suffix}"] += 1
        if int(reservation.custom_ticket_price) > 0:
            result["special_cases"]["custom_ticket_price"] += 1
            if snapshot is None:
                result["special_cases"]["custom_ticket_price_snapshot_null"] += 1
        if is_preopen:
            result["special_cases"]["preopen"] += 1
            if snapshot is None:
                result["special_cases"]["preopen_snapshot_null"] += 1
        if reservation.payment_status == Reservation.PAYMENT_STATUS_WAIVED:
            result["special_cases"]["waived"] += 1
            if snapshot is None:
                result["special_cases"]["waived_snapshot_null"] += 1
        try:
            if reservation.participant_snapshot.participant_type == "family":
                result["special_cases"]["family_reservations"] += 1
        except ObjectDoesNotExist:
            pass
        if account_counts[reservation.user_id] > 1:
            result["special_cases"]["same_account_multiple_reservations"] += 1

        for row in rows:
            if int(row.unit_price_snapshot) != 0:
                continue
            result["special_cases"]["zero_price_consumptions"] += 1
            kind = _zero_price_kind(row, repair_reservation_ids)
            result["special_cases"][kind] += 1

    result["snapshot_mismatches"].sort(key=lambda row: row["reservation_id"])
    result["integrity_findings"].sort(key=lambda row: (row["reservation_id"], row["reason"]))
    result["finding_count"] = len(result["snapshot_mismatches"]) + len(result["integrity_findings"])
    return result
