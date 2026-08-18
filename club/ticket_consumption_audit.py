"""SELECT-only, human-readable TicketConsumption audit."""

from django.utils import timezone

from .lesson_execution_storage import read_status_map
from .models import Reservation, TicketConsumption, TicketPurchase
from .settlement_balance_policy import _execution_slot_key
from .settlement_models import MonthlySettlement
from .settlement_wallet_audit import _display_name, _month_range


PAID_TYPES = {
    TicketPurchase.PURCHASE_TYPE_SINGLE,
    TicketPurchase.PURCHASE_TYPE_SET4,
    TicketPurchase.PURCHASE_TYPE_EVENT,
}


def classify_purchase(purchase):
    if purchase is None:
        # A consumption without a purchase is persisted evidence that tickets
        # were used, but it does not prove their commercial classification.
        return "unverifiable"
    if purchase.purchase_type == TicketPurchase.PURCHASE_TYPE_FORMAL_FREE:
        return "formal_free"
    if purchase.purchase_type == TicketPurchase.PURCHASE_TYPE_LEGACY:
        return "legacy"
    if purchase.purchase_type in PAID_TYPES and int(purchase.unit_price or 0) > 0:
        return "paid"
    if purchase.purchase_type == TicketPurchase.PURCHASE_TYPE_ADMIN:
        return "adjustment"
    return "unknown"


def audit_ticket_consumptions(year, month, *, now=None):
    """Return full detail and count/value summaries without database writes."""
    start, end = _month_range(year, month)
    now = now or timezone.now()
    settlement = MonthlySettlement.objects.filter(year=year, month=month).first()
    execution_map = read_status_map(settlement) if settlement else {}
    consumptions = (
        TicketConsumption.objects.filter(
            reservation__start_at__date__gte=start,
            reservation__start_at__date__lt=end,
        )
        .select_related(
            "user", "purchase", "reservation", "reservation__coach",
            "reservation__substitute_coach", "reservation__availability",
            "reservation__fixed_lesson", "reservation__participant_snapshot",
        )
        .order_by("reservation__start_at", "reservation_id", "id")
    )

    rows = []
    summary = {
        "consumption_count": 0,
        "consumed_ticket_count": 0,
        "paid_ticket_count": 0,
        "formal_free_ticket_count": 0,
        "legacy_ticket_count": 0,
        "adjustment_ticket_count": 0,
        "unverifiable_ticket_count": 0,
        "unknown_ticket_count": 0,
        "returned_ticket_count": 0,
        "future_ticket_count": 0,
        "executed_ticket_count": 0,
        "canceled_ticket_count": 0,
        "executed_paid_consumption_value": 0,
        "future_paid_consumption_value": 0,
        "active_inventory_value": 0,
    }
    for consumption in consumptions:
        reservation = consumption.reservation
        purchase = consumption.purchase
        tickets = int(consumption.tickets_used or 0)
        value = tickets * int(consumption.unit_price_snapshot or 0)
        classification = classify_purchase(purchase)
        returned = consumption.refunded_at is not None
        canceled = reservation.status in {
            Reservation.STATUS_CANCELED, Reservation.STATUS_RAIN_CANCELED,
        }
        is_future = reservation.end_at > now and not canceled
        saved_status = (execution_map.get(_execution_slot_key(reservation)) or {}).get("status")
        is_executed = not returned and not canceled and reservation.end_at <= now and saved_status == "held"
        if returned:
            lifecycle_state = "refunded"
        elif canceled:
            lifecycle_state = "canceled"
        elif is_future:
            lifecycle_state = "future"
        elif is_executed:
            lifecycle_state = "executed"
        else:
            lifecycle_state = "past_unconfirmed"

        participant = getattr(reservation, "participant_snapshot", None)
        row = {
            "consumption_id": consumption.pk,
            "reservation_id": reservation.pk,
            "user_id": consumption.user_id,
            "member_name": _display_name(consumption.user),
            "participant_name": getattr(participant, "participant_name", "") or _display_name(consumption.user),
            "participant_type": getattr(participant, "participant_type", "self"),
            "lesson_date": reservation.start_at.date().isoformat(),
            "start_at": reservation.start_at.isoformat(),
            "end_at": reservation.end_at.isoformat(),
            "occurrence": _execution_slot_key(reservation),
            "coach": _display_name(reservation.coach),
            "substitute_coach": _display_name(reservation.substitute_coach),
            "lesson_type": reservation.lesson_type,
            "lesson_type_label": reservation.get_lesson_type_display(),
            "reservation_status": reservation.status,
            "execution_status": saved_status or ("scheduled" if is_future else "unconfirmed"),
            "tickets_used": tickets,
            "unit_price_snapshot": int(consumption.unit_price_snapshot or 0),
            "consumption_value": value,
            "purchase_id": purchase.pk if purchase else None,
            "purchase_type": purchase.purchase_type if purchase else None,
            "purchase_unit_price": int(purchase.unit_price or 0) if purchase else None,
            "purchase_label": purchase.label if purchase else "",
            "purchase_evidence": "present" if purchase else "missing_purchase_evidence",
            "classification": classification,
            "consumed_at": consumption.created_at.isoformat(),
            "refunded_at": consumption.refunded_at.isoformat() if returned else None,
            "returned": returned,
            "lifecycle_state": lifecycle_state,
        }
        rows.append(row)
        summary["consumption_count"] += 1
        summary["consumed_ticket_count"] += tickets
        summary[f"{classification}_ticket_count"] += tickets
        if returned:
            summary["returned_ticket_count"] += tickets
        elif canceled:
            summary["canceled_ticket_count"] += tickets
        elif is_future:
            summary["future_ticket_count"] += tickets
        elif is_executed:
            summary["executed_ticket_count"] += tickets
        if not returned:
            summary["active_inventory_value"] += value
        if classification == "paid" and not returned and not canceled:
            if is_future:
                summary["future_paid_consumption_value"] += value
            elif is_executed:
                summary["executed_paid_consumption_value"] += value

    return {"year": int(year), "month": int(month), "summary": summary, "consumptions": rows}
