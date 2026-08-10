"""Read-only integrity diagnostics for court transfers and rain refunds."""

from collections import defaultdict

from .expense_metadata import parse_expense_note
from .models import CoachExpense, RainRefund


COURT_TRANSFER_RECORD_KIND = "court_transfer"
REFUND_PENDING = "refund_pending"
REFUNDED = "refunded"


def _integer(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _integer_list(values):
    result = []
    for value in values or []:
        parsed = _integer(value)
        if parsed is not None:
            result.append(parsed)
    return result


def diagnose_court_rain_integrity():
    """Return serializable findings without writing to any database table."""
    transfers = defaultdict(list)
    expense_meta = {}
    expenses = CoachExpense.objects.filter(
        category=CoachExpense.CATEGORY_COURT,
    ).only("id", "amount", "created_by_id", "note").order_by("id")
    for expense in expenses:
        meta = parse_expense_note(expense.note)
        expense_meta[expense.pk] = meta
        if meta.get("record_kind") != COURT_TRANSFER_RECORD_KIND:
            continue
        availability_id = _integer(meta.get("availability_id"))
        if availability_id is None:
            continue
        transfers[availability_id].append((expense, meta))

    duplicate_court_transfers = []
    for availability_id, rows in sorted(transfers.items()):
        if len(rows) < 2:
            continue
        pks = [expense.pk for expense, _meta in rows]
        oldest_pk, latest_pk = min(pks), max(pks)
        duplicate_court_transfers.append(
            {
                "availability_id": availability_id,
                "count": len(rows),
                "expense_ids": pks,
                "amounts": [int(expense.amount) for expense, _meta in rows],
                "created_by_ids": [expense.created_by_id for expense, _meta in rows],
                "payer_coach_ids": [
                    _integer(meta.get("payer_coach_id"))
                    for _expense, meta in rows
                ],
                "using_coach_ids": [
                    _integer_list(meta.get("using_coach_ids"))
                    for _expense, meta in rows
                ],
                "oldest_pk": oldest_pk,
                "latest_pk": latest_pk,
                "registration_selected_pk": oldest_pk,
                "settlement_selected_pk": latest_pk,
                "selection_matches": oldest_pk == latest_pk,
            }
        )

    refunds = list(
        RainRefund.objects.select_related("expense")
        .only(
            "id", "expense_id", "availability_id", "status", "amount",
            "expense__id", "expense__category", "expense__note",
        )
        .order_by("id")
    )
    refunds_by_availability = defaultdict(list)
    state_mismatches = []
    metadata_mismatches = []
    refund_expense_ids = set()
    for refund in refunds:
        refund_expense_ids.add(refund.expense_id)
        if refund.availability_id is not None:
            refunds_by_availability[refund.availability_id].append(refund)

        meta = expense_meta.get(refund.expense_id)
        if meta is None:
            meta = parse_expense_note(refund.expense.note)
        approval = str(meta.get("approval_status") or "")
        if (
            approval == REFUNDED and refund.status == RainRefund.STATUS_PENDING
        ) or (
            approval == REFUND_PENDING and refund.status == RainRefund.STATUS_REFUNDED
        ):
            state_mismatches.append(
                {
                    "rain_refund_id": refund.pk,
                    "expense_id": refund.expense_id,
                    "availability_id": refund.availability_id,
                    "expense_approval_status": approval,
                    "rain_refund_status": refund.status,
                }
            )

        metadata_availability_id = _integer(meta.get("availability_id"))
        reasons = []
        if refund.expense.category != CoachExpense.CATEGORY_COURT:
            reasons.append("expense_category_is_not_court")
        if meta.get("record_kind") != COURT_TRANSFER_RECORD_KIND:
            reasons.append("expense_is_not_court_transfer")
        if metadata_availability_id != refund.availability_id:
            reasons.append("availability_id_mismatch")
        if reasons:
            metadata_mismatches.append(
                {
                    "rain_refund_id": refund.pk,
                    "expense_id": refund.expense_id,
                    "availability_id": refund.availability_id,
                    "metadata_availability_id": metadata_availability_id,
                    "reasons": reasons,
                }
            )

    duplicate_rain_refunds = []
    for availability_id, rows in sorted(refunds_by_availability.items()):
        if len(rows) < 2:
            continue
        duplicate_rain_refunds.append(
            {
                "availability_id": availability_id,
                "count": len(rows),
                "rain_refund_ids": [row.pk for row in rows],
                "expense_ids": [row.expense_id for row in rows],
                "statuses": [row.status for row in rows],
                "amounts": [int(row.amount) for row in rows],
            }
        )

    missing_rain_refunds = []
    for availability_id, rows in sorted(transfers.items()):
        for expense, meta in rows:
            if (
                meta.get("approval_status") in {REFUND_PENDING, REFUNDED}
                and expense.pk not in refund_expense_ids
            ):
                missing_rain_refunds.append(
                    {
                        "availability_id": availability_id,
                        "expense_id": expense.pk,
                        "expense_approval_status": meta.get("approval_status"),
                    }
                )

    result = {
        "duplicate_court_transfers": duplicate_court_transfers,
        "duplicate_rain_refunds": duplicate_rain_refunds,
        "state_mismatches": state_mismatches,
        "metadata_mismatches": metadata_mismatches,
        "missing_rain_refunds": missing_rain_refunds,
    }
    result["finding_count"] = sum(len(rows) for rows in result.values())
    return result
