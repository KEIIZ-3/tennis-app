from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .family_reservations import copy_waitlist_participant_snapshot
from .lesson_participants import CAPACITY_CONSUMING_STATUSES
from .models import CoachAvailability, Court, LessonWaitlist, Reservation, TicketLedger
from .reservation_service import create_reservation


@dataclass(frozen=True)
class WaitlistPromotionResult:
    reservation: Reservation
    converted_existing: bool = False


def _lock_occurrence(waitlist):
    if waitlist.availability_id:
        return CoachAvailability.objects.select_for_update().get(pk=waitlist.availability_id)

    availability = (
        CoachAvailability.objects.select_for_update()
        .filter(
            coach_id=waitlist.coach_id,
            court_id=waitlist.court_id,
            lesson_type=waitlist.lesson_type,
            start_at=waitlist.start_at,
            end_at=waitlist.end_at,
        )
        .first()
    )
    if availability is None:
        Court.objects.select_for_update().get(pk=waitlist.court_id)
    return availability


def _slot_filter(waitlist):
    return {
        "coach_id": waitlist.coach_id,
        "court_id": waitlist.court_id,
        "lesson_type": waitlist.lesson_type,
        "start_at": waitlist.start_at,
        "end_at": waitlist.end_at,
    }


@transaction.atomic
def promote_waitlist(waitlist_id, *, created_by=None):
    """Promote exactly the FIFO head while holding the canonical occurrence lock."""
    candidate = LessonWaitlist.objects.get(pk=waitlist_id)
    availability = _lock_occurrence(candidate)

    waitlist = (
        LessonWaitlist.objects.select_for_update()
        .select_related("user", "coach", "substitute_coach", "court", "fixed_lesson")
        .get(pk=waitlist_id)
    )
    if waitlist.status != LessonWaitlist.STATUS_WAITING:
        raise ValidationError("このキャンセル待ちはすでに処理済みです。")
    if waitlist.start_at <= timezone.now():
        raise ValidationError("開始済み・終了済みのレッスンは繰り上げできません。")
    if availability is not None and availability.is_recruitment_closed:
        raise ValidationError("募集終了中のレッスンは繰り上げできません。")

    slot_filter = _slot_filter(waitlist)
    fifo_head = (
        LessonWaitlist.objects.select_for_update()
        .filter(**slot_filter, status=LessonWaitlist.STATUS_WAITING)
        .order_by("created_at", "id")
        .first()
    )
    if fifo_head is None or fifo_head.pk != waitlist.pk:
        raise ValidationError("先に登録されたキャンセル待ちを先に繰り上げてください。")

    existing = (
        Reservation.objects.filter(
            user_id=waitlist.user_id,
            **slot_filter,
            status__in=CAPACITY_CONSUMING_STATUSES,
        )
        .order_by("id")
        .first()
    )
    if existing is not None:
        waitlist.mark_converted()
        return WaitlistPromotionResult(existing, converted_existing=True)

    reservation = create_reservation(
        user=waitlist.user,
        coach=waitlist.coach,
        substitute_coach=waitlist.substitute_coach,
        court=waitlist.court,
        availability=availability,
        fixed_lesson=waitlist.fixed_lesson,
        lesson_type=waitlist.lesson_type,
        target_level=waitlist.target_level,
        target_level_2=waitlist.target_level_2 or "",
        start_at=waitlist.start_at,
        end_at=waitlist.end_at,
        status=Reservation.STATUS_ACTIVE,
        custom_ticket_price=getattr(availability, "custom_ticket_price", 0) if availability else 0,
        custom_duration_hours=getattr(availability, "custom_duration_hours", 0) if availability else 0,
    )
    copy_waitlist_participant_snapshot(waitlist, reservation)
    reservation.consume_tickets(
        reason=TicketLedger.REASON_RESERVATION_USE,
        created_by=created_by,
        note=f"キャンセル待ち繰り上げ: {reservation.start_at:%Y-%m-%d %H:%M}",
    )
    waitlist.mark_converted()
    return WaitlistPromotionResult(reservation)
