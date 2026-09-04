from django.db.models import Prefetch
from django.utils import timezone

from .lesson_execution_storage import read_status_map
from .lesson_participants import CAPACITY_CONSUMING_STATUSES
from .models import (
    CoachAvailability,
    FixedLesson,
    FixedLessonCanceledOccurrence,
    LessonWaitlist,
    Reservation,
    TicketPurchaseReservation,
)
from .settlement_models import MonthlySettlement
from .ticket_purchase_reservation_service import is_main_coach


def _local_datetime(value):
    if timezone.is_aware(value):
        return timezone.localtime(value)
    return value


def _slot_key(*, lesson_type, coach_id, court_id, start_at, end_at):
    return (lesson_type, coach_id, court_id, start_at, end_at)


def build_lesson_calendar_display_data(*, user, target_year, target_month, month_start, next_month):
    """Fetch and aggregate the data used to render one lesson-calendar month."""
    calendar_settlement = MonthlySettlement.objects.filter(
        year=target_year,
        month=target_month,
    ).first()
    calendar_execution_statuses = (
        read_status_map(calendar_settlement)
        if calendar_settlement is not None
        else {}
    )

    occurrence_reservations = Reservation.objects.filter(
        start_at__date__gte=month_start,
        start_at__date__lt=next_month,
    ).select_related("fixed_lesson", "availability")
    occurrence_statuses = {}
    for reservation in occurrence_reservations:
        local_start = _local_datetime(reservation.start_at)
        if reservation.fixed_lesson_id:
            canceled_key = f"fixed:{reservation.fixed_lesson_id}:{local_start.date().isoformat()}"
        elif reservation.availability_id:
            canceled_key = f"availability:{reservation.availability_id}"
        else:
            continue
        occurrence_statuses.setdefault(canceled_key, []).append(reservation)

    reservation_list = list(
        Reservation.objects.filter(
            status__in=CAPACITY_CONSUMING_STATUSES,
            start_at__date__gte=month_start,
            start_at__date__lt=next_month,
        )
        .select_related("user", "coach", "substitute_coach", "court", "availability", "fixed_lesson")
        .order_by("start_at", "id")
    )

    pending_ticket_purchase_counts_by_user = {}
    if is_main_coach(user):
        for user_id in TicketPurchaseReservation.objects.filter(
            status=TicketPurchaseReservation.STATUS_PENDING,
        ).values_list("user_id", flat=True):
            pending_ticket_purchase_counts_by_user[user_id] = (
                pending_ticket_purchase_counts_by_user.get(user_id, 0) + 1
            )

    user_has_active_family_members = bool(
        user.is_authenticated
        and user.family_member_profiles.filter(is_active=True).exists()
    )
    active_slot_counts = {}
    pending_slot_counts = {}
    fixed_lesson_active_counts = {}
    fixed_lesson_pending_counts = {}
    user_slot_status_map = {}
    user_fixed_lesson_status_map = {}
    reservations_by_availability = {}
    reservations_by_fixed_occurrence = {}
    for reservation in reservation_list:
        slot_key = _slot_key(
            lesson_type=reservation.lesson_type,
            coach_id=reservation.coach_id,
            court_id=reservation.court_id,
            start_at=reservation.start_at,
            end_at=reservation.end_at,
        )
        fixed_key = None
        if reservation.fixed_lesson_id:
            local_date = _local_datetime(reservation.start_at).date().isoformat()
            fixed_key = (str(reservation.fixed_lesson_id), local_date)
            reservations_by_fixed_occurrence.setdefault(
                (reservation.fixed_lesson_id, local_date), []
            ).append(reservation)
        if reservation.availability_id:
            reservations_by_availability.setdefault(reservation.availability_id, []).append(reservation)

        if reservation.status == Reservation.STATUS_ACTIVE:
            active_slot_counts[slot_key] = active_slot_counts.get(slot_key, 0) + 1
            if fixed_key:
                fixed_lesson_active_counts[fixed_key] = fixed_lesson_active_counts.get(fixed_key, 0) + 1
        elif reservation.status == Reservation.STATUS_PENDING:
            pending_slot_counts[slot_key] = pending_slot_counts.get(slot_key, 0) + 1
            if fixed_key:
                fixed_lesson_pending_counts[fixed_key] = fixed_lesson_pending_counts.get(fixed_key, 0) + 1

        if user.is_authenticated and reservation.user_id == user.pk:
            user_slot_status_map[slot_key] = reservation.status
            if fixed_key:
                current_status = user_fixed_lesson_status_map.get(fixed_key, "")
                if reservation.status == Reservation.STATUS_ACTIVE or not current_status:
                    user_fixed_lesson_status_map[fixed_key] = reservation.status

    waitlist_counts = {}
    fixed_lesson_waitlist_counts = {}
    user_waitlist_map = {}
    user_fixed_lesson_waitlist_map = {}
    waitlists = (
        LessonWaitlist.objects.filter(
            status=LessonWaitlist.STATUS_WAITING,
            start_at__date__gte=month_start,
            start_at__date__lt=next_month,
        )
        .select_related("user", "coach", "substitute_coach", "court", "availability", "fixed_lesson")
        .order_by("start_at", "created_at", "id")
    )
    for waitlist in waitlists:
        slot_key = _slot_key(
            lesson_type=waitlist.lesson_type,
            coach_id=waitlist.coach_id,
            court_id=waitlist.court_id,
            start_at=waitlist.start_at,
            end_at=waitlist.end_at,
        )
        waitlist_counts[slot_key] = waitlist_counts.get(slot_key, 0) + 1
        fixed_key = None
        if waitlist.fixed_lesson_id:
            local_date = _local_datetime(waitlist.start_at).date().isoformat()
            fixed_key = (str(waitlist.fixed_lesson_id), local_date)
            fixed_lesson_waitlist_counts[fixed_key] = fixed_lesson_waitlist_counts.get(fixed_key, 0) + 1
        if user.is_authenticated and waitlist.user_id == user.pk:
            user_waitlist_map[slot_key] = waitlist.pk
            if fixed_key:
                user_fixed_lesson_waitlist_map[fixed_key] = waitlist.pk

    fixed_lesson_list = list(
        FixedLesson.objects.filter(is_active=True)
        .select_related("coach", "coach_2", "coach_3", "court")
        .prefetch_related(
            Prefetch(
                "canceled_occurrences",
                queryset=FixedLessonCanceledOccurrence.objects.filter(
                    occurrence_date__gte=month_start,
                    occurrence_date__lt=next_month,
                ).only("fixed_lesson_id", "occurrence_date"),
                to_attr="calendar_canceled_occurrences",
            )
        )
        .order_by("weekday", "start_hour", "id")
    )
    availability_list = list(
        CoachAvailability.objects.filter(
            start_at__date__gte=month_start,
            start_at__date__lt=next_month,
        )
        .select_related("coach", "substitute_coach", "court")
        .order_by("start_at", "coach__username", "court__name", "id")
    )
    availabilities_by_schedule = {}
    for availability in availability_list:
        key = (availability.coach_id, availability.lesson_type, availability.start_at, availability.end_at)
        availabilities_by_schedule.setdefault(key, []).append(availability)

    return {
        "calendar_execution_statuses": calendar_execution_statuses,
        "occurrence_statuses": occurrence_statuses,
        "reservation_list": reservation_list,
        "pending_ticket_purchase_counts_by_user": pending_ticket_purchase_counts_by_user,
        "user_has_active_family_members": user_has_active_family_members,
        "active_slot_counts": active_slot_counts,
        "pending_slot_counts": pending_slot_counts,
        "fixed_lesson_active_counts": fixed_lesson_active_counts,
        "fixed_lesson_pending_counts": fixed_lesson_pending_counts,
        "user_slot_status_map": user_slot_status_map,
        "user_fixed_lesson_status_map": user_fixed_lesson_status_map,
        "waitlist_counts": waitlist_counts,
        "fixed_lesson_waitlist_counts": fixed_lesson_waitlist_counts,
        "user_waitlist_map": user_waitlist_map,
        "user_fixed_lesson_waitlist_map": user_fixed_lesson_waitlist_map,
        "fixed_lesson_list": fixed_lesson_list,
        "availability_list": availability_list,
        "availabilities_by_schedule": availabilities_by_schedule,
        "reservations_by_availability": reservations_by_availability,
        "reservations_by_fixed_occurrence": reservations_by_fixed_occurrence,
    }
