from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Q
from django.shortcuts import render
from django.utils import timezone

from .models import CoachAvailability, FixedLesson, LessonWaitlist, Reservation, StringingOrder, User


def _can_use_admin_dashboard(user):
    if not user or not user.is_authenticated:
        return False
    return (
        getattr(user, "role", "") in User.COACH_ROLE_VALUES
        or bool(getattr(user, "is_staff", False))
        or bool(getattr(user, "is_superuser", False))
    )


def _coach_scope_filter(user):
    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return Q()
    return Q(coach=user) | Q(substitute_coach=user)


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
    if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
        stringing_scope = Q(assigned_coach=user) | Q(assigned_coach__isnull=True)

    unhandled_stringing_orders = StringingOrder.objects.filter(
        stringing_scope,
        status__in=[StringingOrder.STATUS_REQUESTED, StringingOrder.STATUS_IN_PROGRESS],
    )

    unpaid_preopen_reservations = Reservation.objects.filter(
        coach_scope,
        start_at__date=today,
        status=Reservation.STATUS_ACTIVE,
        payment_status=Reservation.PAYMENT_STATUS_UNPAID,
    )

    upcoming_lessons = CoachAvailability.objects.filter(
        coach_scope,
        start_at__gte=now,
        start_at__date__lte=next_week,
    ).select_related("coach", "substitute_coach", "court").order_by("start_at")[:8]

    active_fixed_lessons = FixedLesson.objects.filter(is_active=True)
    if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
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
        "today_reservations": today_reservations.count(),
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
        "is_full_admin": bool(user.is_staff or user.is_superuser),
    }
    return render(request, "coach/admin_dashboard.html", context)
