from django.contrib.auth import get_user_model

from .lesson_participants import CONFIRMED_PARTICIPANT_STATUSES
from .models import CoachExpense, Reservation, StringingOrder, TicketPurchase
from .stringing_service import recognized_stringing_orders


def executed_reservations(reservations, *, settlement, now=None):
    """Return only completed occurrences explicitly confirmed as held."""
    if settlement is None:
        return []
    from django.utils import timezone
    from .lesson_execution_storage import read_status_map
    from .settlement_balance_policy import _execution_slot_key

    status_map = read_status_map(settlement)
    effective_now = now or timezone.now()
    return [
        reservation for reservation in reservations
        if reservation.end_at <= effective_now
        and (status_map.get(_execution_slot_key(reservation)) or {}).get("status")
        == "held"
    ]


def load_monthly_settlement_data(*, month_start, next_month, settlement=None, now=None):
    User = get_user_model()

    coaches = list(
        User.objects.filter(role__in=("coach", "contractor_coach")).order_by(
            "full_name",
            "username",
            "id",
        )
    )

    reservations = list(
        Reservation.objects.filter(
            start_at__date__gte=month_start,
            start_at__date__lt=next_month,
            status__in=CONFIRMED_PARTICIPANT_STATUSES,
        )
        .exclude(
            fixed_lesson__isnull=True,
            availability__note__startswith="固定レッスン:",
        )
        .select_related(
            "user",
            "coach",
            "substitute_coach",
            "court",
            "availability",
            "fixed_lesson",
            "fixed_lesson__coach",
            "fixed_lesson__coach_2",
            "fixed_lesson__coach_3",
        )
        .prefetch_related("ticket_consumptions__purchase")
        .order_by("start_at", "id")
    )
    if settlement is not None:
        reservations = executed_reservations(
            reservations,
            settlement=settlement,
            now=now,
        )

    stringing_orders = list(
        recognized_stringing_orders(
            StringingOrder.objects.all(),
            month_start=month_start,
            next_month=next_month,
        ).select_related("assigned_coach", "user")
    )

    all_expenses = list(
        CoachExpense.objects.filter(expense_date__lt=next_month)
        .select_related("created_by")
        .order_by("expense_date", "id")
    )

    ticket_purchases = list(
        TicketPurchase.objects.filter(
            purchased_at__date__gte=month_start,
            purchased_at__date__lt=next_month,
        ).exclude(purchase_type=TicketPurchase.PURCHASE_TYPE_LEGACY)
    )

    return {
        "coaches": coaches,
        "reservations": reservations,
        "stringing_orders": stringing_orders,
        "all_expenses": all_expenses,
        "ticket_purchases": ticket_purchases,
    }
