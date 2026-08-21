from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .lesson_participants import reservations_for_lesson
from .models import ParticipantPriceChange, Reservation


def participant_name(reservation):
    if reservation.guest_name:
        return f"ゲスト：{reservation.guest_name}"
    snapshot = getattr(reservation, "participant_snapshot", None)
    if snapshot and snapshot.participant_name:
        return snapshot.participant_name
    return reservation.user.display_name() if reservation.user_id else "ゲスト"


def participation_revenue(reservation):
    if reservation.status != Reservation.STATUS_ACTIVE:
        return 0
    amount = reservation.participant_ticket_price_snapshot
    return None if amount is None else int(amount)


def validate_amount(value):
    try:
        amount = int(value)
    except (TypeError, ValueError):
        raise ValidationError("金額は0以上の整数で入力してください。")
    if amount < 0:
        raise ValidationError("金額は0以上で入力してください。")
    return amount


@transaction.atomic
def add_guest(*, actor, guest_name, coach, court, start_at, end_at,
              lesson_type, amount, capacity, availability=None,
              fixed_lesson=None, target_level="beginner"):
    guest_name = (guest_name or "").strip()
    if not guest_name:
        raise ValidationError("ゲスト氏名を入力してください。")
    amount = validate_amount(amount)
    list(Reservation.objects.select_for_update().filter(start_at=start_at, end_at=end_at))
    current = reservations_for_lesson(
        fixed_lesson=fixed_lesson, availability=availability, coach=coach,
        court=court, lesson_type=lesson_type, start_at=start_at, end_at=end_at,
        statuses=(Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING),
    ).count()
    if current >= int(capacity):
        raise ValidationError("定員に達しているためゲストを追加できません。")
    reservation = Reservation.objects.create(
        user=None, guest_name=guest_name, coach=coach, court=court,
        availability=availability, fixed_lesson=fixed_lesson,
        lesson_type=lesson_type, target_level=target_level or "beginner",
        start_at=start_at, end_at=end_at, tickets_used=1,
        participant_ticket_price_snapshot=amount, status=Reservation.STATUS_ACTIVE,
    )
    ParticipantPriceChange.objects.create(
        reservation=reservation, participant_name=f"ゲスト：{guest_name}",
        old_amount=0, new_amount=amount, changed_by=actor,
    )
    return reservation


@transaction.atomic
def change_participation_amount(*, reservation_id, amount, actor):
    reservation = Reservation.objects.select_for_update(of=("self",)).get(pk=reservation_id)
    amount = validate_amount(amount)
    old_amount = reservation.participant_ticket_price_snapshot
    if old_amount != amount:
        reservation.participant_ticket_price_snapshot = amount
        reservation.save(update_fields=["participant_ticket_price_snapshot"])
        ParticipantPriceChange.objects.create(
            reservation=reservation, participant_name=participant_name(reservation),
            old_amount=old_amount, new_amount=amount, changed_by=actor,
        )
    return reservation


@transaction.atomic
def cancel_guest(*, reservation_id):
    reservation = Reservation.objects.select_for_update().get(pk=reservation_id, user__isnull=True)
    if reservation.status == Reservation.STATUS_ACTIVE:
        reservation.status = Reservation.STATUS_CANCELED
        reservation.canceled_at = timezone.now()
        reservation.cancellation_reason = "ゲスト誤登録・参加キャンセル"
        reservation.save(update_fields=["status", "canceled_at", "cancellation_reason"])
    return reservation
