from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .lesson_participants import reservations_for_lesson
from .models import (
    CoachAvailability,
    ParticipantPriceChange,
    Reservation,
    TicketLedger,
    User,
    ensure_accounting_month_is_open,
)


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


def _recalculate_reservation_month(start_at):
    local_start = timezone.localtime(start_at) if timezone.is_aware(start_at) else start_at
    ensure_accounting_month_is_open(local_start)

    from .settlement_service import calculate_monthly_settlement

    calculate_monthly_settlement(local_start.year, local_start.month, force=True)


@transaction.atomic
def add_guest(*, actor, guest_name, coach, court, start_at, end_at,
              lesson_type, amount, capacity, availability=None,
              fixed_lesson=None, target_level="beginner"):
    guest_name = (guest_name or "").strip()
    if not guest_name:
        raise ValidationError("ゲスト氏名を入力してください。")
    amount = validate_amount(amount)
    ensure_accounting_month_is_open(start_at)
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
    _recalculate_reservation_month(reservation.start_at)
    return reservation


@transaction.atomic
def add_member_to_lesson_occurrence(*, actor, member, availability, fixed_lesson=None):
    """Record an administrator-confirmed attendee on one existing occurrence."""
    if not actor or not getattr(actor, "is_authenticated", False):
        raise ValidationError("参加者を追加する権限がありません。")
    if not (
        actor.is_staff
        or actor.is_superuser
        or getattr(actor, "role", "") in User.COACH_ROLE_VALUES
    ):
        raise ValidationError("参加者を追加する権限がありません。")
    if not member or not member.is_active or member.role not in User.LESSON_PARTICIPANT_ROLE_VALUES:
        raise ValidationError("参加可能な会員を選択してください。")

    locked_availability = (
        CoachAvailability.objects.select_for_update(of=("self",))
        .select_related("coach", "substitute_coach", "court", "fixed_lesson_source")
        .get(pk=availability.pk)
    )
    ensure_accounting_month_is_open(locked_availability.start_at)

    from . import lesson_execution

    status = lesson_execution.status_by_availability(
        actor,
        {(locked_availability.start_at.year, locked_availability.start_at.month)},
    ).get(locked_availability.pk, {}).get("execution_status")
    allowed_statuses = {
        lesson_execution.STATUS_SCHEDULED,
        lesson_execution.STATUS_UNCONFIRMED,
        lesson_execution.STATUS_HELD,
    }
    if status not in allowed_statuses:
        raise ValidationError(
            "開催予定・実施確認待ち・実施済みのレッスンにのみ会員を追加できます。"
        )

    canonical_fixed_lesson = fixed_lesson or locked_availability.fixed_lesson_source
    occurrence_reservations = reservations_for_lesson(
        fixed_lesson=canonical_fixed_lesson,
        availability=locked_availability,
        coach=locked_availability.coach,
        court=locked_availability.court,
        lesson_type=locked_availability.lesson_type,
        start_at=locked_availability.start_at,
        end_at=locked_availability.end_at,
        statuses=(Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING),
    )
    if occurrence_reservations.filter(user=member).exists():
        raise ValidationError("この会員はすでにこのレッスンに参加登録されています。")
    if occurrence_reservations.count() >= max(
        int(locked_availability.effective_capacity()),
        int(locked_availability.capacity or 0),
    ):
        raise ValidationError("このレッスンは満員のため会員を追加できません。")

    reservation = Reservation(
        user=member,
        coach=locked_availability.coach,
        substitute_coach=locked_availability.substitute_coach,
        court=locked_availability.court,
        availability=locked_availability,
        fixed_lesson=canonical_fixed_lesson,
        lesson_type=locked_availability.lesson_type,
        target_level=locked_availability.target_level,
        target_level_2=locked_availability.target_level_2,
        start_at=locked_availability.start_at,
        end_at=locked_availability.end_at,
        custom_ticket_price=locked_availability.custom_ticket_price,
        custom_duration_hours=locked_availability.custom_duration_hours,
        status=Reservation.STATUS_ACTIVE,
    )
    reservation._allow_admin_attendance_level_override = True
    reservation.save()
    reservation.consume_tickets(
        reason=TicketLedger.REASON_RESERVATION_USE,
        created_by=actor,
        note="管理者による参加者事後追加",
    )

    _recalculate_reservation_month(locked_availability.start_at)
    return reservation


@transaction.atomic
def change_participation_amount(*, reservation_id, amount, actor):
    reservation = Reservation.objects.select_for_update(of=("self",)).get(pk=reservation_id)
    amount = validate_amount(amount)
    ensure_accounting_month_is_open(reservation.start_at)
    old_amount = reservation.participant_ticket_price_snapshot
    if old_amount != amount:
        reservation.participant_ticket_price_snapshot = amount
        reservation.save(update_fields=["participant_ticket_price_snapshot"])
        ParticipantPriceChange.objects.create(
            reservation=reservation, participant_name=participant_name(reservation),
            old_amount=old_amount, new_amount=amount, changed_by=actor,
        )
        _recalculate_reservation_month(reservation.start_at)
    return reservation


@transaction.atomic
def cancel_guest(*, reservation_id):
    reservation = Reservation.objects.select_for_update().get(pk=reservation_id, user__isnull=True)
    ensure_accounting_month_is_open(reservation.start_at)
    if reservation.status == Reservation.STATUS_ACTIVE:
        reservation.status = Reservation.STATUS_CANCELED
        reservation.canceled_at = timezone.now()
        reservation.cancellation_reason = "ゲスト誤登録・参加キャンセル"
        reservation.save(update_fields=["status", "canceled_at", "cancellation_reason"])
        _recalculate_reservation_month(reservation.start_at)
    return reservation
