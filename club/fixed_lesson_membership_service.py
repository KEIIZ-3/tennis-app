from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from .family_reservations import (
    PARTICIPANT_SELF,
    resolve_reservation_participant,
    save_reservation_participant_snapshot,
)
from .models import CoachAvailability, FixedLesson, LessonWaitlist, Reservation


MEMBER_CANCEL_REASON = "会員が予約確認画面からキャンセル"
MEMBER_REMOVED_REASON = "固定レッスンメンバー解除"
OCCURRENCE_REMOVED_REASON = "固定レッスンの開催回数変更による自動整理"
DUPLICATE_RESERVATION_REASON = "固定メンバー予約との重複整理"


def _is_intentionally_canceled(fixed_lesson, member, start_at, end_at):
    return Reservation.objects.filter(
        user=member,
        fixed_lesson=fixed_lesson,
        is_fixed_entry=True,
        start_at=start_at,
        end_at=end_at,
    ).filter(
        models.Q(status=Reservation.STATUS_RAIN_CANCELED)
        | models.Q(
            status=Reservation.STATUS_CANCELED,
            cancellation_reason=MEMBER_CANCEL_REASON,
        )
    ).exists()


def _fixed_note(fixed_lesson):
    return f"固定レッスン: {fixed_lesson.title or fixed_lesson.get_weekday_display()}"


def _canonical_availability(fixed_lesson, start_at, end_at, required_capacity):
    """同一コーチ・種別・日時を1つの開催枠として扱う。"""
    primary_coach = fixed_lesson.primary_coach()
    candidates = list(
        CoachAvailability.objects.select_for_update()
        .filter(
            coach=primary_coach,
            lesson_type=fixed_lesson.lesson_type,
            start_at=start_at,
            end_at=end_at,
        )
        .order_by("id")
    )

    if candidates:
        availability = candidates[0]
    else:
        availability = CoachAvailability(
            coach=primary_coach,
            court=fixed_lesson.court,
            lesson_type=fixed_lesson.lesson_type,
            start_at=start_at,
            end_at=end_at,
            capacity=required_capacity,
            coach_count=fixed_lesson.coach_count,
            court_count=fixed_lesson.court_count,
            target_level=fixed_lesson.target_level,
            target_level_2=fixed_lesson.target_level_2,
            note=_fixed_note(fixed_lesson),
        )
        availability.save()
        candidates = [availability]

    desired_values = {
        "court": fixed_lesson.court,
        "capacity": required_capacity,
        "coach_count": fixed_lesson.coach_count,
        "court_count": fixed_lesson.court_count,
        "target_level": fixed_lesson.target_level,
        "target_level_2": fixed_lesson.target_level_2,
        "note": _fixed_note(fixed_lesson),
    }
    updated_fields = []
    for field_name, desired_value in desired_values.items():
        current_id = getattr(availability, f"{field_name}_id", None)
        desired_id = getattr(desired_value, "pk", None)
        changed = (
            current_id != desired_id
            if desired_id is not None
            else getattr(availability, field_name) != desired_value
        )
        if changed:
            setattr(availability, field_name, desired_value)
            updated_fields.append(field_name)
    if updated_fields:
        availability.save(update_fields=updated_fields)

    duplicate_ids = [item.pk for item in candidates[1:]]
    if duplicate_ids:
        Reservation.objects.filter(
            availability_id__in=duplicate_ids,
            fixed_lesson=fixed_lesson,
            start_at=start_at,
            end_at=end_at,
            status__in=[Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING],
        ).update(
            coach=primary_coach,
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
            availability_id__in=duplicate_ids,
            fixed_lesson=fixed_lesson,
            start_at=start_at,
            end_at=end_at,
            status=LessonWaitlist.STATUS_WAITING,
        ).update(
            coach=primary_coach,
            court=fixed_lesson.court,
            availability=availability,
            lesson_type=fixed_lesson.lesson_type,
            target_level=fixed_lesson.target_level,
            target_level_2=fixed_lesson.target_level_2,
            substitute_coach=availability.substitute_coach,
        )

        for duplicate in candidates[1:]:
            has_links = (
                Reservation.objects.filter(availability=duplicate).exists()
                or LessonWaitlist.objects.filter(availability=duplicate).exists()
            )
            if not has_links and (duplicate.note or "").startswith("固定レッスン:"):
                duplicate.delete()

    return availability


def _ensure_self_snapshot(reservation):
    participant = resolve_reservation_participant(reservation.user, PARTICIPANT_SELF)
    save_reservation_participant_snapshot(reservation, participant)


def _active_occurrence_reservations(fixed_lesson, member, availability, start_at, end_at):
    return list(
        Reservation.objects.select_for_update()
        .filter(
            user=member,
            lesson_type=fixed_lesson.lesson_type,
            start_at=start_at,
            end_at=end_at,
            status__in=[Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING],
        )
        .filter(
            models.Q(fixed_lesson=fixed_lesson)
            | models.Q(availability=availability)
            | models.Q(
                coach=fixed_lesson.primary_coach(),
                court=fixed_lesson.court,
            )
        )
        .order_by("-is_fixed_entry", "id")
        .distinct()
    )


def _cancel_duplicate_reservations(reservations, canonical, created_by=None):
    for reservation in reservations:
        if reservation.pk == canonical.pk:
            continue
        reservation.cancel(
            created_by=created_by,
            reason=DUPLICATE_RESERVATION_REASON,
        )


def _create_or_update_reservation(
    fixed_lesson,
    member,
    availability,
    start_at,
    end_at,
    created_by=None,
):
    occurrence_reservations = _active_occurrence_reservations(
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

        canonical = Reservation(
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
        canonical.full_clean()
        canonical.save()
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
        updated_fields = []
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
                updated_fields.append(field_name)
        if updated_fields:
            canonical.save(update_fields=updated_fields)

        _cancel_duplicate_reservations(
            occurrence_reservations,
            canonical,
            created_by=created_by,
        )

    _ensure_self_snapshot(canonical)
    return canonical


def synchronize_fixed_lesson_membership(fixed_lesson_id, created_by=None):
    """FixedLesson.members を正本として、開催枠・参加者・予約を原子的に一致させる。"""
    with transaction.atomic():
        fixed_lesson = (
            FixedLesson.objects.select_for_update()
            .select_related("coach", "coach_2", "coach_3", "court")
            .get(pk=fixed_lesson_id)
        )

        if not fixed_lesson.is_active:
            return 0
        if not fixed_lesson.court_id:
            raise ValidationError("固定メンバーの予約生成にはコート設定が必要です。")

        today = timezone.localdate()
        target_dates = [
            target_date
            for target_date in fixed_lesson.scheduled_occurrence_dates()
            if target_date >= today
        ]
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
                reservation = _create_or_update_reservation(
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
