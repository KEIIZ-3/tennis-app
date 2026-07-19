from datetime import date, timedelta

from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Count, Max, Q, Sum
from django.shortcuts import render
from django.utils import timezone

from .models import Reservation, TicketPurchase, User


def _can_view_analytics(user):
    if not user or not user.is_authenticated:
        return False
    return bool(
        getattr(user, "is_staff", False)
        or getattr(user, "is_superuser", False)
        or getattr(user, "role", "") == User.ROLE_COACH
    )


def _period_from_request(request):
    today = timezone.localdate()
    period = (request.GET.get("period") or "month").strip()

    if period == "today":
        return period, today, today, "今日"

    if period == "year":
        return period, date(today.year, 1, 1), date(today.year, 12, 31), f"{today.year}年"

    if period == "custom":
        try:
            start_date = date.fromisoformat((request.GET.get("start") or "").strip())
            end_date = date.fromisoformat((request.GET.get("end") or "").strip())
            if start_date > end_date:
                start_date, end_date = end_date, start_date
            return period, start_date, end_date, f"{start_date:%Y/%m/%d}〜{end_date:%Y/%m/%d}"
        except Exception:
            period = "month"

    month_start = date(today.year, today.month, 1)
    if today.month == 12:
        next_month = date(today.year + 1, 1, 1)
    else:
        next_month = date(today.year, today.month + 1, 1)
    month_end = next_month - timedelta(days=1)
    return "month", month_start, month_end, f"{today.year}年{today.month}月"


@login_required
@user_passes_test(_can_view_analytics)
def analytics_dashboard(request):
    period, start_date, end_date, period_label = _period_from_request(request)

    reservations = Reservation.objects.filter(
        start_at__date__gte=start_date,
        start_at__date__lte=end_date,
        status=Reservation.STATUS_ACTIVE,
    ).select_related("coach", "substitute_coach", "user")

    cash_paid = reservations.filter(
        payment_status=Reservation.PAYMENT_STATUS_PAID,
    ).aggregate(total=Sum("payment_amount"))["total"] or 0

    ticket_purchases = TicketPurchase.objects.filter(
        purchased_at__date__gte=start_date,
        purchased_at__date__lte=end_date,
    )
    ticket_sales = sum(
        int(item.total_tickets or 0) * int(item.unit_price or 0)
        for item in ticket_purchases.only("total_tickets", "unit_price")
    )

    lesson_keys = set()
    lesson_hours = 0
    coach_map = {}
    lesson_type_map = {}

    for reservation in reservations:
        assigned_coach = reservation.substitute_coach or reservation.coach
        coach_id = getattr(assigned_coach, "pk", None)
        coach_name = assigned_coach.display_name() if assigned_coach else "未設定"
        lesson_key = (
            reservation.availability_id or 0,
            reservation.start_at,
            reservation.end_at,
            coach_id or 0,
            reservation.court_id or 0,
            reservation.lesson_type,
        )
        if lesson_key not in lesson_keys:
            lesson_keys.add(lesson_key)
            lesson_hours += max(int(reservation.duration_hours() or 0), 0)

        coach_row = coach_map.setdefault(
            coach_id or 0,
            {
                "name": coach_name,
                "lesson_keys": set(),
                "participants": 0,
                "cash_sales": 0,
                "tickets_used": 0,
            },
        )
        coach_row["lesson_keys"].add(lesson_key)
        coach_row["participants"] += 1
        coach_row["tickets_used"] += int(reservation.tickets_used or 0)
        if reservation.payment_status == Reservation.PAYMENT_STATUS_PAID:
            coach_row["cash_sales"] += int(reservation.payment_amount or 0)

        lesson_type_row = lesson_type_map.setdefault(
            reservation.lesson_type,
            {
                "label": reservation.get_lesson_type_display(),
                "participants": 0,
                "lesson_keys": set(),
            },
        )
        lesson_type_row["participants"] += 1
        lesson_type_row["lesson_keys"].add(lesson_key)

    coach_rows = []
    for row in coach_map.values():
        coach_rows.append(
            {
                "name": row["name"],
                "lesson_count": len(row["lesson_keys"]),
                "participants": row["participants"],
                "cash_sales": row["cash_sales"],
                "tickets_used": row["tickets_used"],
            }
        )
    coach_rows.sort(key=lambda row: (-row["participants"], row["name"]))

    lesson_type_rows = []
    for row in lesson_type_map.values():
        lesson_type_rows.append(
            {
                "label": row["label"],
                "lesson_count": len(row["lesson_keys"]),
                "participants": row["participants"],
            }
        )
    lesson_type_rows.sort(key=lambda row: (-row["participants"], row["label"]))

    members = User.objects.filter(role=User.ROLE_MEMBER, is_active=True)
    thirty_days_ago = timezone.localdate() - timedelta(days=30)
    inactive_member_count = members.annotate(
        last_reservation=Max(
            "reservations__start_at",
            filter=Q(reservations__status=Reservation.STATUS_ACTIVE),
        )
    ).filter(
        Q(last_reservation__isnull=True) | Q(last_reservation__date__lt=thirty_days_ago)
    ).count()

    low_ticket_count = members.filter(ticket_balance__lte=1).count()
    level_rows = list(
        members.values("member_level")
        .annotate(count=Count("id"))
        .order_by("member_level")
    )
    for row in level_rows:
        row["label"] = User.level_label(row["member_level"])

    context = {
        "period": period,
        "period_label": period_label,
        "start_date": start_date,
        "end_date": end_date,
        "summary": {
            "lesson_count": len(lesson_keys),
            "participant_count": reservations.count(),
            "lesson_hours": lesson_hours,
            "cash_sales": int(cash_paid),
            "ticket_sales": int(ticket_sales),
            "recorded_sales": int(cash_paid) + int(ticket_sales),
            "ticket_purchase_count": ticket_purchases.count(),
            "member_count": members.count(),
            "inactive_member_count": inactive_member_count,
            "low_ticket_count": low_ticket_count,
        },
        "coach_rows": coach_rows,
        "lesson_type_rows": lesson_type_rows,
        "level_rows": level_rows,
    }
    return render(request, "coach/analytics_dashboard.html", context)
