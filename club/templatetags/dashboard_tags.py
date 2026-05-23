from datetime import timedelta

from django import template
from django.db import models
from django.utils import timezone

from club.models import Reservation, StringingOrder

register = template.Library()


def _is_staff_like(user):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    return getattr(user, "role", None) in ("coach", "admin", "staff", "manager")


def _is_coach(user):
    return bool(user and getattr(user, "is_authenticated", False) and getattr(user, "role", None) == "coach")


def _future_reservations_for_member(user):
    if not user or not getattr(user, "is_authenticated", False):
        return Reservation.objects.none()

    now = timezone.now()
    return (
        Reservation.objects.select_related("coach", "substitute_coach", "court", "user")
        .filter(
            user=user,
            start_at__gte=now,
            status__in=[Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING],
        )
        .order_by("start_at", "id")
    )


def _reservations_for_coach(user):
    if not user or not getattr(user, "is_authenticated", False):
        return Reservation.objects.none()

    qs = Reservation.objects.select_related("user", "coach", "substitute_coach", "court")

    if _is_staff_like(user) and not _is_coach(user):
        return qs

    return qs.filter(models.Q(coach=user) | models.Q(substitute_coach=user)).distinct()


@register.simple_tag
def member_next_reservation(user):
    try:
        return _future_reservations_for_member(user).first()
    except Exception:
        return None


@register.simple_tag
def member_pending_reservation_count(user):
    try:
        return _future_reservations_for_member(user).filter(status=Reservation.STATUS_PENDING).count()
    except Exception:
        return 0


@register.simple_tag
def member_upcoming_reservation_count(user):
    try:
        return _future_reservations_for_member(user).count()
    except Exception:
        return 0


@register.simple_tag
def coach_pending_request_count(user):
    try:
        return (
            _reservations_for_coach(user)
            .filter(
                status=Reservation.STATUS_PENDING,
                lesson_type__in=[Reservation.LESSON_PRIVATE, Reservation.LESSON_GROUP],
            )
            .count()
        )
    except Exception:
        return 0


@register.simple_tag
def coach_pending_requests_preview(user, limit=3):
    try:
        return list(
            _reservations_for_coach(user)
            .filter(
                status=Reservation.STATUS_PENDING,
                lesson_type__in=[Reservation.LESSON_PRIVATE, Reservation.LESSON_GROUP],
            )
            .order_by("start_at", "created_at", "id")[: int(limit or 3)]
        )
    except Exception:
        return []


@register.simple_tag
def coach_today_reservation_count(user):
    try:
        now = timezone.localtime()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start = today_start + timedelta(days=1)

        return (
            _reservations_for_coach(user)
            .filter(
                status=Reservation.STATUS_ACTIVE,
                start_at__gte=today_start,
                start_at__lt=tomorrow_start,
            )
            .count()
        )
    except Exception:
        return 0


@register.simple_tag
def coach_unhandled_stringing_count(user):
    try:
        qs = StringingOrder.objects.filter(
            status__in=[StringingOrder.STATUS_REQUESTED, StringingOrder.STATUS_IN_PROGRESS]
        )

        if _is_staff_like(user) and not _is_coach(user):
            return qs.count()

        return qs.filter(assigned_coach=user).count()
    except Exception:
        return 0


@register.simple_tag
def coach_rain_cancel_candidate_count(user):
    try:
        now = timezone.now()
        return (
            _reservations_for_coach(user)
            .filter(status=Reservation.STATUS_ACTIVE, start_at__gte=now)
            .count()
        )
    except Exception:
        return 0



@register.simple_tag
def member_low_ticket_warning(user, threshold=2):
    try:
        balance = int(getattr(user, "ticket_balance", 0) or 0)
        return balance <= int(threshold or 2)
    except Exception:
        return False



@register.simple_tag
def member_next_reservation_status_label(user):
    try:
        reservation = member_next_reservation(user)
        if not reservation:
            return ""
        return reservation.get_status_display()
    except Exception:
        return ""
