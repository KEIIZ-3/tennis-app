from django.utils import timezone

from .lesson_participants import CANCELED_RESERVATION_STATUSES
from .lesson_participants import reservations_for_object
from .models import LessonWaitlist, Reservation
from .templatetags.family_participant_tags import (
    participant_for_reservation,
    participant_for_waitlist,
)


def get_reservation_detail_reservation(*, reservation_id):
    """Load the reservation and relations required by the detail display."""
    return (
        Reservation.objects.select_related(
            "user",
            "coach",
            "substitute_coach",
            "court",
            "availability",
            "fixed_lesson",
        )
        .prefetch_related("ticket_consumptions__purchase", "ticket_ledgers")
        .get(pk=reservation_id)
    )


def build_reservation_detail_display(
    *, reservation, user, can_user_cancel_reservation,
    coach_can_manage_request, capacity_for_reservation_slot,
    coach_can_manage_waitlist, user_can_manage_waitlist, display_name, now=None,
):
    """Build the read-only context for a reservation detail page."""
    now = now or timezone.now()
    can_cancel, cancel_reason = can_user_cancel_reservation(user, reservation)

    ticket_consumption_rows = [
        {
            "consumption": consumption,
            "unit_price_label": consumption.unit_price_label(),
            "is_refunded": bool(consumption.refunded_at),
        }
        for consumption in reservation.ticket_consumptions.select_related("purchase").order_by(
            "created_at", "id"
        )
    ]
    ticket_ledger_rows = list(
        reservation.ticket_ledgers.select_related("created_by").order_by(
            "-created_at", "-id"
        )[:20]
    )

    same_slot_reservations = list(
        reservations_for_object(reservation).select_related(
            "user", "coach", "substitute_coach", "court"
        )
    )
    same_slot_reservation_rows = [
        {
            "reservation": same_reservation,
            "participant": participant_for_reservation(same_reservation),
        }
        for same_reservation in same_slot_reservations
    ]

    same_slot_waitlists = list(
        LessonWaitlist.objects.select_related(
            "user", "coach", "substitute_coach", "court", "availability", "fixed_lesson"
        )
        .filter(
            coach=reservation.coach,
            court=reservation.court,
            lesson_type=reservation.lesson_type,
            start_at=reservation.start_at,
            end_at=reservation.end_at,
        )
        .order_by("status", "created_at", "id")
    )

    capacity = capacity_for_reservation_slot(reservation)
    active_count = len(same_slot_reservations)
    waitlist_rows = []
    for waitlist in same_slot_waitlists:
        waitlist_rows.append(
            {
                "waitlist": waitlist,
                "participant": participant_for_waitlist(waitlist),
                "can_promote": (
                    waitlist.status == LessonWaitlist.STATUS_WAITING
                    and reservation.start_at >= now
                    and active_count < capacity
                    and coach_can_manage_waitlist(user, waitlist)
                ),
                "can_cancel": (
                    waitlist.status == LessonWaitlist.STATUS_WAITING
                    and waitlist.start_at >= now
                    and user_can_manage_waitlist(user, waitlist)
                ),
            }
        )

    return {
        "reservation": reservation,
        "participant": participant_for_reservation(reservation),
        "can_cancel": can_cancel,
        "can_customer_cancel": reservation.status in (
            Reservation.STATUS_ACTIVE,
            Reservation.STATUS_PENDING,
        ) and (
            reservation.user_id == user.pk or user.is_staff or user.is_superuser
        ),
        "cancel_reason": cancel_reason,
        "can_manage_request": coach_can_manage_request(user, reservation)
        and reservation.status == Reservation.STATUS_PENDING,
        "assigned_coach_name": reservation.assigned_coach_display(),
        "normal_coach_name": reservation.normal_coach_display(),
        "substitute_coach_name": display_name(reservation.substitute_coach)
        if reservation.substitute_coach else "",
        "has_substitute": reservation.has_substitute_coach(),
        "ticket_consumption_rows": ticket_consumption_rows,
        "ticket_ledger_rows": ticket_ledger_rows,
        "same_slot_reservations": same_slot_reservations,
        "same_slot_reservation_rows": same_slot_reservation_rows,
        "same_slot_waitlist_rows": waitlist_rows,
        "slot_capacity": capacity,
        "slot_active_count": active_count,
        "slot_remaining_count": max(capacity - active_count, 0),
    }


def build_member_reservation_list_display(
    *, user, can_user_cancel_reservation, active_reservation_count_for_slot,
    capacity_for_waitlist_slot, user_can_manage_waitlist,
    coach_can_manage_waitlist, display_name, now=None,
):
    """Build the read-only context for a member's reservation confirmation list."""
    now = now or timezone.now()
    reservations = (
        Reservation.objects.select_related(
            "user", "coach", "substitute_coach", "court", "availability", "fixed_lesson"
        )
        .prefetch_related("ticket_consumptions__purchase")
        .filter(user=user)
        .order_by("start_at", "id")
    )
    waitlists = (
        LessonWaitlist.objects.select_related(
            "user", "coach", "substitute_coach", "court", "availability", "fixed_lesson"
        )
        .filter(user=user)
        .order_by("start_at", "created_at", "id")
    )

    future_rows, past_rows, canceled_rows = [], [], []
    for reservation in reservations:
        can_cancel, cancel_reason = can_user_cancel_reservation(user, reservation)
        row = {
            "reservation": reservation,
            "can_cancel": can_cancel,
            "can_customer_cancel": reservation.status in (
                Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING
            ) and (reservation.user_id == user.pk or user.is_staff or user.is_superuser),
            "cancel_reason": cancel_reason,
            "assigned_coach_name": reservation.assigned_coach_display(),
            "normal_coach_name": reservation.normal_coach_display(),
            "substitute_coach_name": display_name(reservation.substitute_coach)
            if reservation.substitute_coach else "",
            "has_substitute": reservation.has_substitute_coach(),
            "is_future": reservation.start_at >= now,
            "is_canceled": reservation.status in CANCELED_RESERVATION_STATUSES,
            "is_pending": reservation.status == Reservation.STATUS_PENDING,
            "is_active": reservation.status == Reservation.STATUS_ACTIVE,
        }
        if row["is_canceled"]:
            canceled_rows.append(row)
        elif row["is_future"]:
            future_rows.append(row)
        else:
            past_rows.append(row)

    waitlist_rows = []
    for waitlist in waitlists:
        active_count = active_reservation_count_for_slot(
            coach=waitlist.coach, court=waitlist.court,
            lesson_type=waitlist.lesson_type, start_at=waitlist.start_at,
            end_at=waitlist.end_at,
        )
        capacity = capacity_for_waitlist_slot(waitlist)
        waitlist_rows.append({
            "waitlist": waitlist,
            "can_cancel": waitlist.status == LessonWaitlist.STATUS_WAITING
            and waitlist.start_at >= now and user_can_manage_waitlist(user, waitlist),
            "can_promote": waitlist.status == LessonWaitlist.STATUS_WAITING
            and waitlist.start_at >= now and active_count < capacity
            and coach_can_manage_waitlist(user, waitlist),
            "active_count": active_count,
            "capacity": capacity,
            "remaining_count": max(capacity - active_count, 0),
            "assigned_coach_name": waitlist.assigned_coach_display(),
            "normal_coach_name": display_name(waitlist.coach),
            "substitute_coach_name": display_name(waitlist.substitute_coach)
            if waitlist.substitute_coach else "",
            "has_substitute": bool(waitlist.substitute_coach_id),
        })

    waiting_rows = [
        row for row in waitlist_rows
        if row["waitlist"].status == LessonWaitlist.STATUS_WAITING
    ]
    processed_rows = [
        row for row in waitlist_rows
        if row["waitlist"].status != LessonWaitlist.STATUS_WAITING
    ]
    return {
        "future_reservation_rows": future_rows,
        "past_reservation_rows": past_rows,
        "canceled_reservation_rows": canceled_rows,
        "waiting_waitlist_rows": waiting_rows,
        "processed_waitlist_rows": processed_rows,
        "reservation_rows": future_rows + past_rows + canceled_rows,
        "waitlist_rows": waitlist_rows,
    }
