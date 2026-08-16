from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from .fixed_lesson_membership_service import (
    DUPLICATE_RESERVATION_REASON,
    MEMBER_REMOVED_REASON,
    OCCURRENCE_REMOVED_REASON,
    _canonical_availability,
    _ensure_self_snapshot,
    _is_intentionally_canceled,
    rebind_occurrence_links,
)
from .models import Court, FixedLesson, LessonWaitlist, Reservation
from .reservation_service import create_reservation


UNASSIGNED_COURT_NAME = "コート未定（後日決定）"


def configured_future_dates(fixed_lesson, reference_date=None):
    """管理画面で設定された開催日から、未到来の開催日だけを返す。

    FixedLesson.start_date と weeks_ahead は、管理画面に表示される
    「繰り返し開始日」と「作成する開催回数」の正本である。
    現在日を基準に新しい開催回を後ろへ延長しない。
    """
    reference_date = reference_date or timezone.localdate()
    return [
        target_date
        for target_date in fixed_lesson.scheduled_occurrence_dates()
        if target_date >= reference_date
    ]


def _ensure_booking_court(fixed_lesson_id):
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


def _locked_occurrence_reservations(
    fixed_lesson,
    member,
    availability,
    start_at,
    end_at,
):
    """候補ID抽出と行ロックを分離し、PostgreSQL制約を回避する。"""
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


def _create_or_update_fixed_reservation(
    fixed_lesson,
    member,
    availability,
    start_at,
    end_at,
    created_by=None,
):
    occurrence_reservations = _locked_occurrence_reservations(
        fixed_lesson,
        member,
        availability,
        start_at,
        end_at,
    )
    canonical = next(
        (
            reservation
            for reservation in occurrence_reservations
            if reservation.is_fixed_entry
            and reservation.fixed_lesson_id == fixed_lesson.pk
        ),
        None,
    )

    if canonical is None:
        for reservation in occurrence_reservations:
            reservation.cancel(
                created_by=created_by,
                reason=DUPLICATE_RESERVATION_REASON,
            )

        canonical = create_reservation(
            user=member,
            coach=fixed_lesson.primary_coach(),
            substitute_coach=availability.substitute_coach,
            court=fixed_lesson.court,
            availability=availability,
            fixed_lesson=fixed_lesson,
            is_fixed_entry=True,
            lesson_type=fixed_lesson.lesson_type,
            target_level=fixed_lesson.target_level,
            target_level_2=fixed_lesson.target_level_2,
            start_at=start_at,
            end_at=end_at,
            status=Reservation.STATUS_ACTIVE,
            custom_ticket_price=availability.custom_ticket_price,
            custom_duration_hours=availability.custom_duration_hours,
        )
    else:
        desired_values = {
            "coach": fixed_lesson.primary_coach(),
            "substitute_coach": availability.substitute_coach,
            "court": fixed_lesson.court,
            "availability": availability,
            "fixed_lesson": fixed_lesson,
            "is_fixed_entry": True,
            "lesson_type": fixed_lesson.lesson_type,
            "target_level": fixed_lesson.target_level,
            "target_level_2": fixed_lesson.target_level_2,
            "custom_ticket_price": availability.custom_ticket_price,
            "custom_duration_hours": availability.custom_duration_hours,
        }
        update_fields = []
        for field_name, desired_value in desired_values.items():
            current_id = getattr(canonical, f"{field_name}_id", None)
            desired_id = getattr(desired_value, "pk", None)
            changed = (
                current_id != desired_id
                if desired_id is not None
                else getattr(canonical, field_name) != desired_value
            )
            if changed:
                setattr(canonical, field_name, desired_value)
                update_fields.append(field_name)
        if update_fields:
            canonical.save(update_fields=update_fields)

        for reservation in occurrence_reservations:
            if reservation.pk == canonical.pk:
                continue
            reservation.cancel(
                created_by=created_by,
                reason=DUPLICATE_RESERVATION_REASON,
            )

    _ensure_self_snapshot(canonical)
    return canonical


def _synchronize_locked_fixed_lesson(fixed_lesson_id, created_by=None):
    fixed_lesson = FixedLesson.objects.select_for_update().get(pk=fixed_lesson_id)

    if not fixed_lesson.is_active:
        canceled_count = 0
        future_reservations = Reservation.objects.select_for_update().filter(
            fixed_lesson=fixed_lesson,
            is_fixed_entry=True,
            start_at__date__gte=timezone.localdate(),
            status=Reservation.STATUS_ACTIVE,
        )
        for reservation in future_reservations:
            if reservation.cancel(
                created_by=created_by,
                reason=OCCURRENCE_REMOVED_REASON,
            ):
                canceled_count += 1
        return canceled_count
    if not fixed_lesson.court_id:
        raise ValidationError("固定メンバーの予約生成にはコート設定が必要です。")

    today = timezone.localdate()
    target_dates = configured_future_dates(fixed_lesson, today)
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
        rebind_occurrence_links(
            fixed_lesson,
            availability,
            start_at,
            end_at,
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
            reservation = _create_or_update_fixed_reservation(
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
    """固定メンバー同期の唯一の入口。"""
    with transaction.atomic():
        _ensure_booking_court(fixed_lesson_id)
        return _synchronize_locked_fixed_lesson(
            fixed_lesson_id,
            created_by=created_by,
        )
