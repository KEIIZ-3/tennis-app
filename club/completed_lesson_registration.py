import hashlib

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .expense_metadata import (
    EXPENSE_APPROVAL_APPROVED,
    EXPENSE_TYPE_COURT_TRANSFER,
    build_expense_note,
)
from .lesson_execution_storage import clear_status, save_status
from .lesson_execution_storage import read_status_map
from . import lesson_execution
from .models import (
    CoachAvailability,
    CoachExpense,
    CompletedLessonRegistration,
    Court,
    Reservation,
    TicketLedger,
    User,
    ensure_accounting_month_is_open,
)
from .settlement_models import MonthlySettlement
from .settlement_service import calculate_monthly_settlement, get_or_create_monthly_settlement


def can_manage_completed_lessons(user, coach=None):
    if not user or not user.is_authenticated:
        return False
    if user.is_staff or user.is_superuser:
        return coach is None or getattr(coach, "role", "") in User.COACH_ROLE_VALUES
    return getattr(user, "role", "") in User.COACH_ROLE_VALUES and (
        coach is None or coach.pk == user.pk
    )


def _canceled_conflict_ids(*, coach, court, start_at, end_at):
    """Return only overlapping occurrences with canonical non-held evidence.

    This is deliberately scoped to completed-lesson registration. Normal
    CoachAvailability validation remains strict.
    """
    candidates = list(
        CoachAvailability.objects.filter(
            start_at__lt=end_at,
            end_at__gt=start_at,
        ).filter(
            Q(coach=coach)
            | Q(coach_2=coach)
            | Q(court=court)
        ).prefetch_related("reservations", "rain_refunds")
    )
    settlements = {
        (row.year, row.month): read_status_map(row)
        for row in MonthlySettlement.objects.filter(
            year__in={row.start_at.year for row in candidates},
            month__in={row.start_at.month for row in candidates},
        )
    }
    excluded = []
    cancellation_statuses = {
        lesson_execution.STATUS_RAIN_CANCELED,
        lesson_execution.STATUS_REFUND_PENDING,
        lesson_execution.STATUS_REFUNDED,
    }
    for candidate in candidates:
        reservations = list(candidate.reservations.all())
        if any(row.status in (Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING) for row in reservations):
            continue
        entry = settlements.get((candidate.start_at.year, candidate.start_at.month), {}).get(
            f"availability:{candidate.pk}", {}
        )
        status, cancellation_type = lesson_execution.effective_status(
            entry, reservations, end_at=candidate.end_at
        )
        has_explicit_cancellation = bool(
            status in cancellation_statuses
            and (cancellation_type in ("rain", "other") or candidate.rain_refunds.exists())
        )
        if has_explicit_cancellation:
            excluded.append(candidate.pk)
    return excluded


def _court_transfer_note(*, registration, availability, payer, amount, actor, canceled=False):
    meta = {
        "expense_type": EXPENSE_TYPE_COURT_TRANSFER,
        "receipt_status": "none",
        "receipt_check_status": "checked",
        "approval_status": EXPENSE_APPROVAL_APPROVED,
        "record_kind": "court_transfer",
        "availability_id": availability.pk,
        "completed_registration_id": registration.pk,
        "payer_coach_id": payer.pk,
        "payer_coach_name": payer.display_name(),
        "using_coach_ids": [availability.coach_id],
        "using_coach_names": [availability.coach.display_name()],
        "recorded_by_id": actor.pk,
        "recorded_by_name": actor.display_name(),
        "completed_registration_canceled": canceled,
    }
    return build_expense_note(meta, "事後登録取消" if canceled else f"事後登録コート代 {amount:,}円")


@transaction.atomic
def register_completed_lesson(*, actor, start_at, end_at, lesson_type, coach, court,
                              participants, court_cost, court_payer, note, idempotency_key):
    if not can_manage_completed_lessons(actor, coach):
        raise ValidationError("この担当コーチの実績を登録する権限がありません。")
    if end_at > timezone.now():
        raise ValidationError("終了日時が現在時刻以前のレッスンだけ登録できます。")
    if start_at >= end_at:
        raise ValidationError("開始時刻は終了時刻より前にしてください。")
    if lesson_type not in dict(Reservation.LESSON_TYPE_CHOICES):
        raise ValidationError("レッスン種別が不正です。")
    if not 1 <= len(participants) <= 10:
        raise ValidationError("顧客人数は1〜10名で指定してください。")
    if not can_manage_completed_lessons(actor, court_payer):
        raise ValidationError("このコート支払者を指定する権限がありません。")
    try:
        normalized_court_cost = int(court_cost or 0)
    except (TypeError, ValueError):
        raise ValidationError("コート代は0以上の整数で入力してください。")
    if normalized_court_cost < 0:
        raise ValidationError("コート代は0以上で入力してください。")
    ensure_accounting_month_is_open(start_at)
    normalized_key = hashlib.sha256(str(idempotency_key).encode("utf-8")).hexdigest()
    existing = CompletedLessonRegistration.objects.select_for_update().filter(
        idempotency_key=normalized_key
    ).first()
    if existing:
        return existing, False
    Court.objects.select_for_update().get(pk=court.pk)
    User.objects.select_for_update().get(pk=coach.pk)
    availability = CoachAvailability(
        coach=coach, court=court, lesson_type=lesson_type, start_at=start_at,
        end_at=end_at, capacity=len(participants), target_level=User.LEVEL_ALL,
        custom_duration_hours=max(int((end_at-start_at).total_seconds() // 3600), 1),
        is_recruitment_closed=True, status=CoachAvailability.STATUS_APPROVED,
        note=(note or "").strip(),
    )
    availability._validated_conflict_exclusion_ids = _canceled_conflict_ids(
        coach=coach, court=court, start_at=start_at, end_at=end_at
    )
    availability.save()
    if availability.capacity != len(participants):
        CoachAvailability.objects.filter(pk=availability.pk).update(capacity=len(participants))
        availability.capacity = len(participants)
    registration = CompletedLessonRegistration.objects.create(
        availability=availability, idempotency_key=normalized_key,
        note=(note or "").strip(), created_by=actor,
    )
    seen_members = set()
    for item in participants:
        user = item.get("user")
        guest_name = (item.get("guest_name") or "").strip()
        method = item.get("payment_method")
        value = int(item.get("value") or 0)
        if user:
            if user.pk in seen_members:
                raise ValidationError("同じ会員を同一開催回へ二重登録できません。")
            seen_members.add(user.pk)
        elif not guest_name:
            raise ValidationError("ゲスト氏名を入力してください。")
        if not user and method == Reservation.PAYMENT_METHOD_TICKET:
            raise ValidationError("ゲストはチケット精算を選択できません。")
        if value <= 0:
            raise ValidationError("チケット枚数または金額は1以上で入力してください。")
        reservation = Reservation(
            user=user, guest_name=guest_name, coach=coach, court=court,
            availability=availability, lesson_type=lesson_type,
            target_level=User.LEVEL_ALL, start_at=start_at, end_at=end_at,
            status=Reservation.STATUS_ACTIVE,
            custom_duration_hours=availability.custom_duration_hours,
        )
        reservation.save()
        if method == Reservation.PAYMENT_METHOD_TICKET:
            Reservation.objects.filter(pk=reservation.pk).update(tickets_used=value)
            reservation.tickets_used = value
            reservation.consume_tickets(
                reason=TicketLedger.REASON_RESERVATION_USE, created_by=actor,
                note="実施済みレッスン事後登録",
            )
        else:
            Reservation.objects.filter(pk=reservation.pk).update(
                tickets_used=0, participant_ticket_price_snapshot=None,
                payment_method=Reservation.PAYMENT_METHOD_CASH,
                payment_status=Reservation.PAYMENT_STATUS_PAID,
                payment_amount=value, payment_received_at=start_at,
                payment_note="実施済みレッスン事後登録",
            )
    if normalized_court_cost > 0:
        expense = CoachExpense(
            expense_date=timezone.localtime(start_at).date(), category=CoachExpense.CATEGORY_COURT,
            amount=normalized_court_cost, created_by=court_payer,
            note=_court_transfer_note(registration=registration, availability=availability,
                                      payer=court_payer, amount=normalized_court_cost, actor=actor),
        )
        expense.full_clean(); expense.save()
    settlement = get_or_create_monthly_settlement(start_at.year, start_at.month)
    save_status(settlement, f"availability:{availability.pk}", "held", actor)
    calculate_monthly_settlement(start_at.year, start_at.month, force=True)
    return registration, True


@transaction.atomic
def cancel_completed_lesson(*, registration_id, actor):
    registration = CompletedLessonRegistration.objects.select_for_update().select_related(
        "availability__coach", "availability__court"
    ).get(pk=registration_id)
    availability = CoachAvailability.objects.select_for_update().get(pk=registration.availability_id)
    if not can_manage_completed_lessons(actor, availability.coach):
        raise ValidationError("この実施済み登録を取り消す権限がありません。")
    if registration.canceled_at:
        return registration, False
    ensure_accounting_month_is_open(availability.start_at)
    reservations = list(Reservation.objects.select_for_update().filter(availability=availability))
    if not reservations:
        raise ValidationError("取消対象の参加者データが見つかりません。")
    for reservation in reservations:
        reservation.cancel(created_by=actor, reason="実施済み登録取消", schedule_notification=False)
    prior_expense = CoachExpense.objects.filter(category=CoachExpense.CATEGORY_COURT).order_by("-id")
    from .court_transfer_service import court_transfer_availability_id
    prior_expense = next((row for row in prior_expense if court_transfer_availability_id(row) == availability.pk), None)
    if prior_expense and int(prior_expense.amount or 0) > 0:
        reversal = CoachExpense(
            expense_date=timezone.localtime(availability.start_at).date(),
            category=CoachExpense.CATEGORY_COURT, amount=0, created_by=prior_expense.created_by,
            note=_court_transfer_note(registration=registration, availability=availability,
                                      payer=prior_expense.created_by, amount=0, actor=actor, canceled=True),
        )
        reversal.full_clean(); reversal.save()
    settlement = get_or_create_monthly_settlement(availability.start_at.year, availability.start_at.month)
    clear_status(settlement, f"availability:{availability.pk}")
    registration.canceled_at = timezone.now(); registration.canceled_by = actor
    registration.save(update_fields=["canceled_at", "canceled_by"])
    calculate_monthly_settlement(availability.start_at.year, availability.start_at.month, force=True)
    return registration, True
