from datetime import date

from django.db.models import Sum
from django.utils import timezone

from .court_policy_reconciliation import (
    _scheduled_fixed_lesson_match,
    reconcile_court_policy,
)
from .court_transfer_service import current_court_transfer_rows
from .models import (
    CoachAvailability,
    RainRefund,
    Reservation,
    StringingOrder,
    TicketConsumption,
    TicketPurchase,
    User,
)
from .settlement_balance_policy import (
    _approved_monthly_expenses,
    _automatic_court_cost,
    _build_court_cost_policy,
    _slot_key_for_reservation,
    _execution_slot_key,
    main_coaches,
)
from .settlement_models import MonthlySettlement, SettlementPayment
from .stringing_service import recognized_stringing_orders, stringing_revenue_amount


def _month_range(year, month):
    start = date(int(year), int(month), 1)
    end = date(int(year) + (int(month) == 12), int(month) % 12 + 1, 1)
    return start, end


def _money(value):
    return int(value or 0)


def _display_name(user):
    if user is None:
        return ""
    try:
        return user.display_name()
    except Exception:
        return str(user)


_EXPLICIT_FREE_MARKERS = ("無料", "無償", "免除", "free")


def _zero_price_classification(consumption, *, refunded, canceled, is_past):
    """Classify zero snapshots from persisted evidence, never from current prices."""
    purchase = consumption.purchase
    evidence_text = " ".join(
        value.strip() for value in (purchase.label or "", purchase.note or "")
        if value and value.strip()
    )
    normalized_evidence = evidence_text.lower()
    explicit_free = any(marker in normalized_evidence for marker in _EXPLICIT_FREE_MARKERS)

    if refunded or canceled:
        classification = "B_refunded_or_canceled"
    elif not is_past:
        classification = "C_future_inventory_only"
    elif int(purchase.unit_price or 0) > 0:
        classification = "E_paid_price_evidence"
    elif explicit_free:
        classification = "A_legitimate_zero_price"
    elif purchase.purchase_type == TicketPurchase.PURCHASE_TYPE_LEGACY:
        classification = "D_historical_unknown_price"
    else:
        classification = "F_inconsistent_or_missing_price_evidence"

    if int(consumption.unit_price_snapshot or 0) > 0:
        source = "ticket_consumption.unit_price_snapshot"
        price_evidence = True
    elif int(purchase.unit_price or 0) > 0:
        source = "ticket_purchase.unit_price"
        price_evidence = True
    elif explicit_free:
        source = "ticket_purchase.label_or_note_explicit_free"
        price_evidence = True
    else:
        source = "no_persisted_price_evidence"
        price_evidence = False
    return classification, source, price_evidence, evidence_text


def _court_cost_audit_rows(year, month, court_policy):
    """Explain every approved court transfer without changing settlement data."""
    start, end = _month_range(year, month)
    expense_rows = [
        row
        for row in _approved_monthly_expenses(start, end)
        if row["is_court"] and row["meta"].get("record_kind") == "court_transfer"
    ]
    rows_without_availability, current_by_occurrence, occurrence_keys = (
        current_court_transfer_rows(expense_rows)
    )

    availability_ids = {
        availability_id for availability_id in occurrence_keys
    }
    for row in expense_rows:
        try:
            availability_ids.add(int(row["meta"].get("availability_id")))
        except (TypeError, ValueError):
            pass
    availability_map = {
        item.pk: item
        for item in CoachAvailability.objects.filter(pk__in=availability_ids)
        .select_related("court", "coach", "substitute_coach")
    }
    rain_refund_availability_ids = set(
        RainRefund.objects.filter(
            lesson_date__gte=start,
            lesson_date__lt=end,
            availability_id__isnull=False,
        ).values_list("availability_id", flat=True)
    )
    reservations = list(
        Reservation.objects.filter(
            start_at__date__gte=start,
            start_at__date__lt=end,
        )
        .select_related("fixed_lesson", "court", "availability")
        .order_by("start_at", "id")
    )
    reservations_by_availability = {}
    reservations_by_slot = {}
    for reservation in reservations:
        if reservation.availability_id:
            reservations_by_availability.setdefault(
                reservation.availability_id, reservation
            )
        slot_key = _slot_key_for_reservation(reservation)
        if slot_key:
            reservations_by_slot.setdefault(slot_key, reservation)

    user_ids = set()
    for row in expense_rows:
        user_ids.add(row["payer_id"])
        for value in row["meta"].get("using_coach_ids") or []:
            try:
                user_ids.add(int(value))
            except (TypeError, ValueError):
                pass
    users = {
        user.pk: user
        for user in User.objects.filter(
            pk__in=[value for value in user_ids if value]
        )
    }

    included_policy_rows = {
        _money(row.get("expense_id")): row
        for row in court_policy.get("detail_rows") or []
        if row.get("expense_id")
    }
    current_legacy_by_slot = {}
    for row in rows_without_availability:
        slot_key = str(row["meta"].get("court_refund_slot_key") or "").strip()
        current = current_legacy_by_slot.get(slot_key)
        if slot_key and (
            current is None or row["expense"].pk > current["expense"].pk
        ):
            current_legacy_by_slot[slot_key] = row

    result = []
    for source in expense_rows:
        expense = source["expense"]
        meta = source["meta"]
        try:
            availability_id = int(meta.get("availability_id"))
        except (TypeError, ValueError):
            availability_id = None
        availability = availability_map.get(availability_id)
        slot_key = str(meta.get("court_refund_slot_key") or "").strip()
        reservation = (
            reservations_by_availability.get(availability_id)
            if availability_id
            else reservations_by_slot.get(slot_key)
        )
        occurrence = availability or reservation
        canonical_source = (
            current_by_occurrence.get(occurrence_keys.get(availability_id))
            if availability_id
            else current_legacy_by_slot.get(slot_key)
        )
        canonical_id = (
            canonical_source["expense"].pk
            if canonical_source is not None
            else expense.pk
        )
        is_canonical = expense.pk == canonical_id
        policy_row = included_policy_rows.get(expense.pk)
        included = policy_row is not None
        using_ids = []
        for value in meta.get("using_coach_ids") or []:
            try:
                coach_id = int(value)
            except (TypeError, ValueError):
                continue
            if coach_id not in using_ids:
                using_ids.append(coach_id)

        start_at = getattr(occurrence, "start_at", None)
        end_at = getattr(occurrence, "end_at", None)
        if start_at and timezone.is_aware(start_at):
            start_at = timezone.localtime(start_at)
        if end_at and timezone.is_aware(end_at):
            end_at = timezone.localtime(end_at)
        court = getattr(occurrence, "court", None)
        fixed_lesson_id = getattr(reservation, "fixed_lesson_id", None)
        if fixed_lesson_id is None and availability is not None:
            _coach_ids, fixed_lesson_ids = _scheduled_fixed_lesson_match(
                availability, set(users)
            )
            if len(fixed_lesson_ids) == 1:
                fixed_lesson_id = fixed_lesson_ids[0]
        calculated_cost = _automatic_court_cost(occurrence) if occurrence else None
        if included:
            reason = policy_row.get("included_reason") or "canonical court transfer"
        elif not is_canonical:
            reason = f"superseded by expense_id {canonical_id}"
        elif occurrence is None:
            reason = "canonical occurrence not found"
        else:
            reason = "excluded by settlement execution/cancellation policy"
        reservation_status = getattr(reservation, "status", "")
        if availability_id in rain_refund_availability_ids:
            execution_status = "canceled_court_settlement"
        elif reservation_status == Reservation.STATUS_RAIN_CANCELED:
            execution_status = "rain_canceled"
        elif reservation_status == Reservation.STATUS_CANCELED:
            execution_status = "canceled"
        else:
            execution_status = (policy_row or {}).get("execution_status", "excluded")
        result.append({
            "expense_id": expense.pk,
            "availability_id": availability_id,
            "fixed_lesson_id": fixed_lesson_id,
            "date": (
                start_at.date().isoformat()
                if start_at
                else str(expense.expense_date)
            ),
            "start_at": start_at.isoformat() if start_at else "",
            "end_at": end_at.isoformat() if end_at else "",
            "court_id": getattr(occurrence, "court_id", None),
            "court_name": str(court or ""),
            "court_count": getattr(occurrence, "court_count", None),
            "registered_cost": _money(expense.amount),
            "calculated_cost": calculated_cost,
            "canonical_cost": _money(expense.amount) if included else 0,
            "cost_warning": bool(
                calculated_cost is not None
                and calculated_cost != _money(expense.amount)
            ),
            "payer_id": source["payer_id"],
            "payer_name": _display_name(users.get(source["payer_id"])),
            "using_coach_ids": using_ids,
            "using_coach_names": [_display_name(users.get(pk)) for pk in using_ids],
            "execution_status": execution_status,
            "canonical_occurrence_key": (
                occurrence_keys.get(availability_id, f"availability:{availability_id}")
                if availability_id else slot_key
            ),
            "is_canonical": is_canonical,
            "duplicate_of": None if is_canonical else canonical_id,
            "included": included,
            "included_reason": reason,
            "created_at": expense.created_at.isoformat(),
        })
    return result


def audit_wallet_month(year, month):
    """Return a SELECT-only, event-level explanation of a monthly company wallet."""
    start, end = _month_range(year, month)
    previous_year, previous_month = (year - 1, 12) if month == 1 else (year, month - 1)
    previous = MonthlySettlement.objects.filter(
        year=previous_year, month=previous_month
    ).first()
    settlement = MonthlySettlement.objects.filter(year=year, month=month).first()

    purchase_rows = []
    purchase_total = 0
    excluded_purchase_rows = []
    purchases = TicketPurchase.objects.filter(
        purchased_at__date__gte=start, purchased_at__date__lt=end
    ).select_related("user").order_by("purchased_at", "id")
    for purchase in purchases:
        amount = _money(purchase.total_tickets) * _money(purchase.unit_price)
        row = {
            "purchase_id": purchase.pk,
            "purchased_at": purchase.purchased_at.isoformat(),
            "user_id": purchase.user_id,
            "purchase_type": purchase.purchase_type,
            "tickets": _money(purchase.total_tickets),
            "unit_price": _money(purchase.unit_price),
            "amount": amount,
        }
        if purchase.purchase_type == TicketPurchase.PURCHASE_TYPE_LEGACY:
            row["excluded_reason"] = "legacy/synthetic balance reconstruction"
            excluded_purchase_rows.append(row)
        else:
            purchase_total += amount
            purchase_rows.append(row)

    from .lesson_execution_storage import read_status_map

    consumption_rows = []
    consumption_total = 0
    consumed_inventory_total = 0
    future_consumed_total = 0
    canceled_refunded_total = 0
    zero_price_consumption_total = 0
    zero_price_consumption_count = 0
    zero_price_rows = []
    zero_price_classification_counts = {
        "A_legitimate_zero_price": 0,
        "B_refunded_or_canceled": 0,
        "C_future_inventory_only": 0,
        "D_historical_unknown_price": 0,
        "E_paid_price_evidence": 0,
        "F_inconsistent_or_missing_price_evidence": 0,
    }
    settlement_status = read_status_map(settlement) if settlement else {}
    now = timezone.now()
    consumptions = TicketConsumption.objects.filter(
        reservation__start_at__date__gte=start,
        reservation__start_at__date__lt=end,
    ).select_related(
        "reservation",
        "reservation__availability",
        "reservation__fixed_lesson",
        "purchase",
        "user",
    ).order_by(
        "reservation__start_at", "reservation_id", "id"
    )
    for consumption in consumptions:
        reservation = consumption.reservation
        slot_key = _execution_slot_key(reservation)
        saved_execution_status = (settlement_status.get(slot_key) or {}).get(
            "status"
        )
        is_past = reservation.end_at <= now
        is_executed = saved_execution_status == "held" and is_past
        refunded = consumption.refunded_at is not None
        canceled = reservation.status in (
            Reservation.STATUS_CANCELED,
            Reservation.STATUS_RAIN_CANCELED,
        )
        included = not refunded and not canceled and is_executed
        amount = _money(consumption.tickets_used) * _money(
            consumption.unit_price_snapshot
        )
        if not refunded:
            consumed_inventory_total += amount
        if not refunded and not canceled and not is_past:
            future_consumed_total += amount
        if refunded or canceled:
            canceled_refunded_total += amount
        if consumption.unit_price_snapshot == 0:
            zero_price_consumption_total += amount
            zero_price_consumption_count += 1
        classification, source, price_evidence, purchase_evidence = (
            _zero_price_classification(
                consumption,
                refunded=refunded,
                canceled=canceled,
                is_past=is_past,
            )
        )
        if included:
            consumption_total += amount
        if refunded:
            included_reason = "refunded consumption"
        elif canceled:
            included_reason = "canceled reservation"
        elif not is_past:
            included_reason = "future occurrence; inventory only"
        elif saved_execution_status != "held":
            included_reason = "occurrence not confirmed held"
        else:
            included_reason = "held occurrence revenue"
        audit_row = {
            "consumption_id": consumption.pk,
            "reservation_id": reservation.pk,
            "participant": _display_name(consumption.user),
            "participant_id": consumption.user_id,
            "user_id": consumption.user_id,
            "lesson_date": reservation.start_at.date().isoformat(),
            "start": reservation.start_at.isoformat(),
            "end": reservation.end_at.isoformat(),
            "execution_status": saved_execution_status or (
                "scheduled" if not is_past else "unconfirmed"
            ),
            "reservation_status": reservation.status,
            "tickets": _money(consumption.tickets_used),
            "purchase_id": consumption.purchase_id,
            "purchase_type": consumption.purchase.purchase_type,
            "purchase_amount": (
                _money(consumption.purchase.total_tickets)
                * _money(consumption.purchase.unit_price)
            ),
            "purchase_unit_price": _money(consumption.purchase.unit_price),
            "purchase_ticket_count": _money(consumption.purchase.total_tickets),
            "purchase_remaining": _money(consumption.purchase.remaining_tickets),
            "purchase_label": consumption.purchase.label,
            "purchase_note": consumption.purchase.note,
            "purchase_created_by_id": consumption.purchase.created_by_id,
            "purchase_created_at": consumption.purchase.created_at.isoformat(),
            "reservation_created_at": reservation.created_at.isoformat(),
            "consumption_created_at": consumption.created_at.isoformat(),
            "unit_price_snapshot": _money(consumption.unit_price_snapshot),
            "consumed_value": amount,
            "refunded": refunded,
            "canceled": canceled,
            "is_past": is_past,
            "is_executed": is_executed,
            "included": included,
            "included_reason": included_reason,
            "source": source,
            "price_evidence": price_evidence,
            "purchase_evidence": purchase_evidence,
            "classification": classification,
        }
        consumption_rows.append(audit_row)
        if consumption.unit_price_snapshot == 0:
            zero_price_rows.append(audit_row)
            zero_price_classification_counts[classification] += 1

    direct_cash_rows = list(Reservation.objects.filter(
        start_at__date__gte=start,
        start_at__date__lt=end,
        status=Reservation.STATUS_ACTIVE,
        payment_method=Reservation.PAYMENT_METHOD_CASH,
        payment_status=Reservation.PAYMENT_STATUS_PAID,
    ).values("id", "start_at", "payment_amount", "payment_received_at"))
    direct_cash_total = sum(_money(row["payment_amount"]) for row in direct_cash_rows)

    stringing_rows = []
    stringing_total = 0
    orders = recognized_stringing_orders(
        StringingOrder.objects.all(), month_start=start, next_month=end
    ).order_by("created_at", "id")
    for order in orders:
        amount = _money(stringing_revenue_amount(order))
        stringing_total += amount
        stringing_rows.append({"order_id": order.pk, "amount": amount})

    coaches = main_coaches()
    main_ids = [coach.pk for coach in coaches]
    court_policy = _build_court_cost_policy(year, month, main_ids, main_ids, [])
    court_policy = reconcile_court_policy(
        court_policy,
        main_coach_ids=main_ids,
        eligible_coach_ids=main_ids,
        contractor_coach_ids=[],
    )
    court_rows = _court_cost_audit_rows(year, month, court_policy)
    paid_total = _money(SettlementPayment.objects.filter(
        monthly_settlement=settlement, is_reversed=False
    ).aggregate(total=Sum("amount"))["total"]) if settlement else 0
    opening = _money(previous.closing_balance) if previous else 0
    expected = opening + purchase_total + direct_cash_total + stringing_total - paid_total

    return {
        "year": year,
        "month": month,
        "previous_closing_wallet": opening,
        "ticket_purchase_cash_total": purchase_total,
        "ticket_purchase_rows": purchase_rows,
        "excluded_purchase_rows": excluded_purchase_rows,
        "ticket_consumption_revenue_total": consumption_total,
        "ticket_consumed_inventory_total": consumed_inventory_total,
        "ticket_future_consumed_value": future_consumed_total,
        "ticket_canceled_refunded_value": canceled_refunded_total,
        "ticket_zero_price_consumption_value": zero_price_consumption_total,
        "ticket_zero_price_consumption_count": zero_price_consumption_count,
        "ticket_zero_price_rows": zero_price_rows,
        "ticket_zero_price_classification_counts": zero_price_classification_counts,
        "ticket_revenue_summary": {
            "executed_paid": sum(
                row["consumed_value"] for row in consumption_rows
                if row["included"] and row["unit_price_snapshot"] > 0
            ),
            "executed_zero_price": sum(
                1 for row in zero_price_rows if row["included"]
            ),
            "future_paid": sum(
                row["consumed_value"] for row in consumption_rows
                if not row["is_past"] and not row["refunded"] and not row["canceled"]
            ),
            "future_zero_price": sum(
                1 for row in zero_price_rows
                if not row["is_past"] and not row["refunded"] and not row["canceled"]
            ),
            "refunded": sum(
                1 for row in zero_price_rows if row["refunded"] or row["canceled"]
            ),
            "legacy_unknown": zero_price_classification_counts["D_historical_unknown_price"],
            "admin_unknown": sum(
                1 for row in zero_price_rows
                if row["purchase_type"] == TicketPurchase.PURCHASE_TYPE_ADMIN
                and not row["price_evidence"]
            ),
        },
        "ticket_consumption_rows": consumption_rows,
        "direct_cash_revenue_total": direct_cash_total,
        "direct_cash_rows": direct_cash_rows,
        "stringing_cash_total": stringing_total,
        "stringing_rows": stringing_rows,
        "court_cost_total": court_policy["finalized_court_cost_total"],
        "court_cost_rows": court_rows,
        "included_court_rows": [row for row in court_rows if row["included"]],
        "excluded_court_rows": [row for row in court_rows if not row["included"]],
        "court_cost_invariant": {
            "included_canonical_cost_sum": sum(
                row["canonical_cost"] for row in court_rows if row["included"]
            ),
            "court_cost_total": court_policy["finalized_court_cost_total"],
            "matches": sum(
                row["canonical_cost"] for row in court_rows if row["included"]
            ) == court_policy["finalized_court_cost_total"],
        },
        "settlement_paid_total": paid_total,
        "unpaid_settlement_total": _money(settlement.unpaid_salary_total) if settlement else 0,
        "calculated_closing_wallet": expected,
        "ui_displayed_wallet": _money(settlement.closing_balance) if settlement else None,
        "ui_difference": (_money(settlement.closing_balance) - expected) if settlement else None,
    }
