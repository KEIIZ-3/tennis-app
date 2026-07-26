from django.contrib.auth import get_user_model

from .models import CoachExpense, Reservation, StringingOrder, TicketPurchase


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
            status=Reservation.STATUS_ACTIVE,
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
        StringingOrder.objects.filter(
            created_at__date__gte=month_start,
            created_at__date__lt=next_month,
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
        )
    )

    return {
        "coaches": coaches,
        "reservations": reservations,
        "stringing_orders": stringing_orders,
        "all_expenses": all_expenses,
        "ticket_purchases": ticket_purchases,
    }
