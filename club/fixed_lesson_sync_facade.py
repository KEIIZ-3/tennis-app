from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from . import fixed_lesson_membership_service as membership_service
from .fixed_lesson_membership_service import (
    MEMBER_REMOVED_REASON,
    OCCURRENCE_REMOVED_REASON,
    _canonical_availability,
    _is_intentionally_canceled,
    _rolling_target_dates,
)
from .models import Court, FixedLesson, LessonWaitlist, Reservation


UNASSIGNED_COURT_NAME = "コート未定（後日決定）"


def _ensure_booking_court(fixed_lesson_id):
    """コート未定の固定レッスンにも、予約を表現できる正式な仮コートを割り当てる。"""
    fixed_lesson = FixedLesson.objects.select_for_update().get(pk=fixed_lesson_id)
    if fixed_lesson.court_id:
        return fixed_lesson.court

    placeholder_court, _created = Court.objects.get_or_create(
        name=UNASSIGNED_COURT_NAME,
        defaults={
            "is_active": True,
            "court_type": Court.COURT_OTHER,
        },
    )
    update_fields = []
    if not placeholder_court.is_active:
        placeholder_court.is_active = True
        update_fields.append("is_active")
    if placeholder_court.court_type != Court.COURT_OTHER:
        placeholder_court.court_type = Court.COURT_OTHER
        update_fields.append("court_type")
    if update_fields:
        placeholder_court.save(update_fields=update_fields)

    fixed_lesson.court = placeholder_court
    fixed_lesson.save(update_fields=["court"])
    return placeholder_court


def _locked_active_occurrence_reservations(
    fixed_lesson,
    member,
    availability,
    start_at,
    end_at,
):
    """重複候補のID抽出と行ロックを別SQLに分ける。

    参加者スナップショットとのJOINは同一Reservationを複数行に展開し得るため、
    候補IDの抽出ではDISTINCTが必要になる。一方PostgreSQLはDISTINCT付きSQLへの
    FOR UPDATEを禁止するため、最初にIDだけを確定し、その後Reservation本体だけを
    select_for_updateでロックする。
    """
    candidate_ids = list(
        Reservation.objects.filter(
            user=member,
            lesson_type=fixed_lesson.lesson_type,
            start_at=start_at,
            end_at=end_at,
            status__in=[Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING],
        )
        .filter(
            models.Q(participant_snapshot__participant_type="self")
            | models.Q(participant_snapshot__isnull=True)
        )
        .filter(
            models.Q(fixed_lesson=fixed_lesson)
            | models.Q(availability=availability)
            | models.Q(
                coach=fixed_lesson.primary_coach(),
                court=fixed_lesson.court,
            )
        )
        .values_list("pk", flat=True)
        .distinct()
    )
    if not candidate_ids:
        return []

    return list(
        Reservation.objects.select_for_update()
        .filter(pk__in=candidate_ids)
        .order_by("-is_fixed_entry", "id")
    )


def _synchronize_locked_fixed_lesson(fixed_lesson_id, created_by=None):
    """固定レッスン本体だけを行ロックして、開催枠・参加者・予約を一致させる。"""
    fixed_lesson = FixedLesson.objects.select_for_update().get(pk=fixed_lesson_id)

    if not fixed_lesson.is_active:
        return 0
    if not fixed_lesson.court_id:
        raise ValidationError("固定メンバーの予約生成にはコート設定が必要です。")

    today = timezone.localdate()
    target_dates = _rolling_target_dates(fixed_lesson, today)
    target_datetimes = {
        fixed_lesson._build_datetimes_for_date(target_date)
        for target_date in target_dates
    }
    members = list(fixed_lesson.members.select_for_update().order_by("pk"))
    member_ids = {member.pk for member in members}
    required_capacity = max(fixed_lesson.effective_capacity(), len(members), 1)
    changed_count = 0

    extra_reservations = Reservation.objects.select_for_update().filter(
        fixed_lesson=fixed_lesson,
        is_fixed_entry=True,
        start_at__date__gte=today,
        status=Reservation.STATUS_ACTIVE,
    )
    for reservation in extra_reservations:
        is_target_occurrence = (reservation.start_at, reservation.end_at) in target_datetimes
        is_current_member = reservation.user_id in member_ids
        if is_target_occurrence and is_current_member:
            continue
        reason = MEMBER_REMOVED_REASON if not is_current_member else OCCURRENCE_REMOVED_REASON
        if reservation.cancel(created_by=created_by, reason=reason):
            changed_count += 1

    for target_date in target_dates:
        start_at, end_at = fixed_lesson._build_datetimes_for_date(target_date)
        availability = _canonical_availability(
            fixed_lesson,
            start_at,
            end_at,
            required_capacity,
        )

        Reservation.objects.filter(
            fixed_lesson=fixed_lesson,
            start_at=start_at,
            end_at=end_at,
            status__in=[Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING],
        ).update(
            coach=fixed_lesson.primary_coach(),
            court=fixed_lesson.court,
            availability=availability,
            lesson_type=fixed_lesson.lesson_type,
            target_level=fixed_lesson.target_level,
            target_level_2=fixed_lesson.target_level_2,
            substitute_coach=availability.substitute_coach,
            custom_ticket_price=availability.custom_ticket_price,
            custom_duration_hours=availability.custom_duration_hours,
        )
        LessonWaitlist.objects.filter(
            fixed_lesson=fixed_lesson,
            start_at=start_at,
            end_at=end_at,
            status=LessonWaitlist.STATUS_WAITING,
        ).update(
            coach=fixed_lesson.primary_coach(),
            court=fixed_lesson.court,
            availability=availability,
            lesson_type=fixed_lesson.lesson_type,
            target_level=fixed_lesson.target_level,
            target_level_2=fixed_lesson.target_level_2,
            substitute_coach=availability.substitute_coach,
        )

        for member in members:
            if _is_intentionally_canceled(
                fixed_lesson,
                member,
                start_at,
                end_at,
            ):
                continue
            before_ids = set(
                Reservation.objects.filter(
                    user=member,
                    fixed_lesson=fixed_lesson,
                    is_fixed_entry=True,
                    start_at=start_at,
                    end_at=end_at,
                    status=Reservation.STATUS_ACTIVE,
                ).values_list("pk", flat=True)
            )
            reservation = membership_service._create_or_update_reservation(
                fixed_lesson,
                member,
                availability,
                start_at,
                end_at,
                created_by=created_by,
            )
            if reservation.pk not in before_ids:
                changed_count += 1

    missing = []
    duplicates = []
    for target_date in target_dates:
        start_at, end_at = fixed_lesson._build_datetimes_for_date(target_date)
        for member in members:
            if _is_intentionally_canceled(
                fixed_lesson,
                member,
                start_at,
                end_at,
            ):
                continue
            active_qs = Reservation.objects.filter(
                user=member,
                lesson_type=fixed_lesson.lesson_type,
                start_at=start_at,
                end_at=end_at,
                status__in=[Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING],
            ).filter(
                models.Q(participant_snapshot__participant_type="self")
                | models.Q(participant_snapshot__isnull=True)
            )
            fixed_count = active_qs.filter(
                fixed_lesson=fixed_lesson,
                is_fixed_entry=True,
            ).count()
            if fixed_count != 1:
                missing.append(f"{member.display_name()} / {target_date:%Y-%m-%d}")
            if active_qs.count() != 1:
                duplicates.append(f"{member.display_name()} / {target_date:%Y-%m-%d}")

    if missing:
        raise ValidationError(
            "固定メンバー予約の生成結果が設定と一致しません: " + "、".join(missing)
        )
    if duplicates:
        raise ValidationError(
            "同一参加者の重複予約が解消されていません: " + "、".join(duplicates)
        )

    return changed_count


def synchronize_fixed_lesson_membership(fixed_lesson_id, created_by=None):
    """固定メンバー同期の全入口。"""
    with transaction.atomic():
        _ensure_booking_court(fixed_lesson_id)
        original_loader = membership_service._active_occurrence_reservations
        membership_service._active_occurrence_reservations = _locked_active_occurrence_reservations
        try:
            return _synchronize_locked_fixed_lesson(
                fixed_lesson_id,
                created_by=created_by,
            )
        finally:
            membership_service._active_occurrence_reservations = original_loader
