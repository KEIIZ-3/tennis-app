from django.contrib.auth import get_user_model

from .lesson_participants import CONFIRMED_PARTICIPANT_STATUSES
from .lesson_execution_storage import read_status_map
from .models import CoachExpense, Reservation, StringingOrder, TicketCashReceipt
from .settlement_models import MonthlySettlement
from .stringing_service import recognized_stringing_orders


def load_monthly_settlement_data(*, month_start, next_month):
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

    ticket_cash_receipts = list(
        TicketCashReceipt.objects.filter(
            received_at__date__gte=month_start,
            received_at__date__lt=next_month,
            reversed_at__isnull=True,
        ).select_related("ticket_purchase")
    )

    settlement = MonthlySettlement.objects.filter(
        year=month_start.year,
        month=month_start.month,
    ).first()

    return {
        "coaches": coaches,
        "reservations": reservations,
        "stringing_orders": stringing_orders,
        "all_expenses": all_expenses,
        "ticket_cash_receipts": ticket_cash_receipts,
        "execution_status_map": read_status_map(settlement) if settlement else {},
    }
