from datetime import date

from django.db.models import Sum

from .models import Reservation, StringingOrder, TicketConsumption, TicketPurchase
from .settlement_balance_policy import _build_court_cost_policy, main_coaches
from .settlement_models import MonthlySettlement, SettlementPayment
from .stringing_service import recognized_stringing_orders, stringing_revenue_amount


def _month_range(year, month):
    start = date(int(year), int(month), 1)
    end = date(int(year) + (int(month) == 12), int(month) % 12 + 1, 1)
    return start, end


def _money(value):
    return int(value or 0)


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

    consumption_rows = []
    consumption_total = 0
    consumptions = TicketConsumption.objects.filter(
        reservation__start_at__date__gte=start,
        reservation__start_at__date__lt=end,
    ).select_related("reservation", "purchase", "user").order_by(
        "reservation__start_at", "reservation_id", "id"
    )
    for consumption in consumptions:
        reservation = consumption.reservation
        included = (
            consumption.refunded_at is None
            and reservation.status == Reservation.STATUS_ACTIVE
        )
        amount = _money(consumption.tickets_used) * _money(
            consumption.unit_price_snapshot
        )
        if included:
            consumption_total += amount
        consumption_rows.append({
            "consumption_id": consumption.pk,
            "reservation_id": reservation.pk,
            "participant_id": consumption.user_id,
            "occurrence": reservation.start_at.isoformat(),
            "tickets": _money(consumption.tickets_used),
            "purchase_id": consumption.purchase_id,
            "purchase_type": consumption.purchase.purchase_type,
            "unit_price_snapshot": _money(consumption.unit_price_snapshot),
            "consumed_value": amount,
            "refunded": consumption.refunded_at is not None,
            "canceled": reservation.status != Reservation.STATUS_ACTIVE,
            "included": included,
            "reason": "active consumption" if included else "refunded or canceled",
        })

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
        "ticket_consumption_rows": consumption_rows,
        "direct_cash_revenue_total": direct_cash_total,
        "direct_cash_rows": direct_cash_rows,
        "stringing_cash_total": stringing_total,
        "stringing_rows": stringing_rows,
        "court_cost_total": court_policy["finalized_court_cost_total"],
        "court_cost_rows": court_policy["detail_rows"],
        "settlement_paid_total": paid_total,
        "unpaid_settlement_total": _money(settlement.unpaid_salary_total) if settlement else 0,
        "calculated_closing_wallet": expected,
        "ui_displayed_wallet": _money(settlement.closing_balance) if settlement else None,
        "ui_difference": (_money(settlement.closing_balance) - expected) if settlement else None,
    }
