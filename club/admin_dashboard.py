from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Q, Sum
from django.shortcuts import render
from django.utils import timezone

from . import lesson_execution
from .models import CoachAvailability, FixedLesson, LessonWaitlist, Reservation, StringingOrder, User
from .settlement_service import get_or_create_monthly_settlement


def _can_use_admin_dashboard(user):
    if not user or not user.is_authenticated:
        return False
    return (
        getattr(user, "role", "") in User.COACH_ROLE_VALUES
        or bool(getattr(user, "is_staff", False))
        or bool(getattr(user, "is_superuser", False))
    )


def _is_full_admin(user):
    return bool(
        getattr(user, "is_staff", False)
        or getattr(user, "is_superuser", False)
    )


def _coach_scope_filter(user):
    if _is_full_admin(user):
        return Q()
    return Q(coach=user) | Q(substitute_coach=user)


def _slot_is_in_scope(slot, user):
    if _is_full_admin(user):
        return True

    fixed_lesson = slot.get("fixed_lesson")
    if fixed_lesson is not None:
        coach_ids = {
            getattr(fixed_lesson, "coach_id", None),
            getattr(fixed_lesson, "coach_2_id", None),
            getattr(fixed_lesson, "coach_3_id", None),
        }
        return user.pk in coach_ids

    availability = slot.get("availability")
    if availability is None:
        return False

    return user.pk in {
        getattr(availability, "coach_id", None),
        getattr(availability, "substitute_coach_id", None),
    }


@login_required
@user_passes_test(_can_use_admin_dashboard)
def admin_dashboard(request):
    user = request.user
    now = timezone.now()
    today = timezone.localdate()
    tomorrow = today + timezone.timedelta(days=1)
    next_week = today + timezone.timedelta(days=7)
    coach_scope = _coach_scope_filter(user)

    today_reservations = Reservation.objects.filter(
        coach_scope,
        start_at__date=today,
        status=Reservation.STATUS_ACTIVE,
    )

    pending_reservations = Reservation.objects.filter(
        coach_scope,
        status=Reservation.STATUS_PENDING,
    )

    waiting_waitlists = LessonWaitlist.objects.filter(
        coach_scope,
        status=LessonWaitlist.STATUS_WAITING,
    )

    stringing_scope = Q()
    if not _is_full_admin(user):
        stringing_scope = Q(assigned_coach=user) | Q(assigned_coach__isnull=True)

    unhandled_stringing_orders = StringingOrder.objects.filter(
        stringing_scope,
        status__in=[StringingOrder.STATUS_REQUESTED, StringingOrder.STATUS_IN_PROGRESS],
    )

    unpaid_preopen_reservations = today_reservations.filter(
        payment_status=Reservation.PAYMENT_STATUS_UNPAID,
    )

    paid_today_total = int(
        today_reservations.filter(
            payment_status=Reservation.PAYMENT_STATUS_PAID,
        ).aggregate(total=Sum("payment_amount"))["total"]
        or 0
    )

    today_slot_keys = set()
    for reservation in today_reservations.values(
        "lesson_type",
        "start_at",
        "end_at",
        "court_id",
        "fixed_lesson_id",
        "availability_id",
    ):
        today_slot_keys.add(
            (
                reservation["lesson_type"],
                reservation["start_at"],
                reservation["end_at"],
                reservation["court_id"],
                reservation["fixed_lesson_id"],
                reservation["availability_id"],
            )
        )

    settlement = get_or_create_monthly_settlement(today.year, today.month)
    status_map = lesson_execution.read_status_map(settlement)
    today_slots = [
        slot
        for slot in lesson_execution._canonical_slots(today.year, today.month)
        if slot["target_date"] == today and _slot_is_in_scope(slot, user)
    ]

    execution_pending_count = 0
    for slot in today_slots:
        entry = lesson_execution._status_entry(status_map, slot)
        status = entry.get("status")
        if slot["end_at"] <= now and status not in {
            lesson_execution.STATUS_HELD,
            lesson_execution.STATUS_RAIN_CANCELED,
            lesson_execution.STATUS_REFUND_PENDING,
            lesson_execution.STATUS_REFUNDED,
        }:
            execution_pending_count += 1

    upcoming_lessons = CoachAvailability.objects.filter(
        coach_scope,
        start_at__gte=now,
        start_at__date__lte=next_week,
    ).select_related("coach", "substitute_coach", "court").order_by("start_at")[:8]

    active_fixed_lessons = FixedLesson.objects.filter(is_active=True)
    if not _is_full_admin(user):
        active_fixed_lessons = active_fixed_lessons.filter(
            Q(coach=user) | Q(coach_2=user) | Q(coach_3=user)
        )

    member_count = User.objects.filter(role=User.ROLE_MEMBER, is_active=True).count()
    low_ticket_member_count = User.objects.filter(
        role=User.ROLE_MEMBER,
        is_active=True,
        ticket_balance__lte=0,
    ).count()

    stats = {
        "today_lesson_count": max(len(today_slots), len(today_slot_keys)),
        "today_participant_count": today_reservations.count(),
        "today_paid_total": paid_today_total,
        "execution_pending_count": execution_pending_count,
        "pending_reservations": pending_reservations.count(),
        "waiting_waitlists": waiting_waitlists.count(),
        "unhandled_stringing_orders": unhandled_stringing_orders.count(),
        "unpaid_preopen_reservations": unpaid_preopen_reservations.count(),
        "active_fixed_lessons": active_fixed_lessons.count(),
        "member_count": member_count,
        "low_ticket_member_count": low_ticket_member_count,
    }

    context = {
        "today": today,
        "tomorrow": tomorrow,
        "stats": stats,
        "upcoming_lessons": upcoming_lessons,
        "is_full_admin": _is_full_admin(user),
    }
    return render(request, "coach/admin_dashboard.html", context)
