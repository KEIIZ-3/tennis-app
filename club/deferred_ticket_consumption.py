"""Link later ticket purchases to already-ledgered ticket usage."""

from django.db import transaction

from .models import Reservation, TicketConsumption, TicketPurchase, User
from .participant_price_snapshot import set_participant_ticket_price_snapshot
from .settlement_models import MonthlySettlement


def _month_is_closed(reservation):
    return MonthlySettlement.objects.filter(
        year=reservation.start_at.year,
        month=reservation.start_at.month,
        status=MonthlySettlement.STATUS_CLOSED,
    ).exists()


def allocate_pending_ticket_consumptions(purchase):
    """Allocate a new lot FIFO without changing balance, ledger, wallet, or court data."""
    with transaction.atomic():
        # Match consume_tickets' user -> purchase lock order to avoid a
        # purchase/booking deadlock under concurrent traffic.
        User.objects.select_for_update().get(pk=purchase.user_id)
        locked_purchase = TicketPurchase.objects.select_for_update().get(pk=purchase.pk)
        available = int(locked_purchase.remaining_tickets or 0)
        if available <= 0:
            return []

        pending = list(
            TicketConsumption.objects.select_for_update()
            .select_related("reservation")
            .filter(
                user_id=locked_purchase.user_id,
                purchase__isnull=True,
                refunded_at__isnull=True,
                reservation__status=Reservation.STATUS_ACTIVE,
                reservation__ticket_refunded_at__isnull=True,
                reservation__ticket_consumed_at__lt=locked_purchase.purchased_at,
            )
            .order_by("reservation__ticket_consumed_at", "id")
        )
        linked = []
        affected_reservation_ids = set()
        for consumption in pending:
            if available <= 0:
                break
            allocated = min(available, int(consumption.tickets_used))
            if allocated == int(consumption.tickets_used):
                consumption.purchase = locked_purchase
                consumption.unit_price_snapshot = locked_purchase.unit_price
                consumption.save(update_fields=["purchase", "unit_price_snapshot"])
                linked_row = consumption
            else:
                consumption.tickets_used -= allocated
                consumption.save(update_fields=["tickets_used"])
                linked_row = TicketConsumption.objects.create(
                    user_id=consumption.user_id,
                    purchase=locked_purchase,
                    reservation_id=consumption.reservation_id,
                    fixed_lesson_id=consumption.fixed_lesson_id,
                    tickets_used=allocated,
                    unit_price_snapshot=locked_purchase.unit_price,
                )
            linked.append(linked_row)
            affected_reservation_ids.add(consumption.reservation_id)
            available -= allocated

        if available != int(locked_purchase.remaining_tickets):
            locked_purchase.remaining_tickets = available
            locked_purchase.save(update_fields=["remaining_tickets"])
            purchase.remaining_tickets = available

        for reservation in Reservation.objects.filter(pk__in=affected_reservation_ids):
            if reservation.participant_ticket_price_snapshot is not None or _month_is_closed(reservation):
                continue
            active_rows = list(
                reservation.ticket_consumptions.filter(refunded_at__isnull=True)
            )
            if active_rows and all(row.purchase_id is not None for row in active_rows):
                set_participant_ticket_price_snapshot(reservation, active_rows)
        return linked
