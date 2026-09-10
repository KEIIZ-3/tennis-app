from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from .family_reservations import (
    PARTICIPANT_SELF,
    resolve_reservation_participant,
    save_reservation_participant_snapshot,
)
from .models import CoachAvailability, FixedLesson, LessonWaitlist, Reservation
from .lesson_participants import CAPACITY_CONSUMING_STATUSES
from .fixed_lesson_occurrence_service import reconcile_fixed_lesson_availability


MEMBER_CANCEL_REASON = "会員が予約確認画面からキャンセル"
MEMBER_REMOVED_REASON = "固定レッスンメンバー解除"
OCCURRENCE_REMOVED_REASON = "固定レッスンの予約生成期間変更による自動整理"
DUPLICATE_RESERVATION_REASON = "固定メンバー予約との重複整理"


def _rolling_target_dates(fixed_lesson, reference_date):
    """有効な固定レッスンについて、基準日以降の予約生成対象日を返す。

    FixedLesson.start_date は固定レッスン自体の開始日であり、終了日ではない。
    weeks_ahead は start_date からの総開催回数ではなく、基準日以降に
    Reservation を実体化しておく回数として扱う。
    """
    repeat_start = getattr(fixed_lesson, "start_date", None) or reference_date
    search_start = max(repeat_start, reference_date)
    weekday = int(getattr(fixed_lesson, "weekday", search_start.weekday()))
    first_offset = (weekday - search_start.weekday()) % 7
    first_date = search_start + timedelta(days=first_offset)

    try:
        occurrence_count = max(int(getattr(fixed_lesson, "weeks_ahead", 1) or 1), 1)
    except Exception:
        occurrence_count = 1

    return [
        first_date + timedelta(days=7 * index)
        for index in range(occurrence_count)
    ]


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


def _canonical_availability(
    fixed_lesson, start_at, end_at, required_capacity, *, occurrence_court=None
):
    """同一コーチ・種別・日時を1つの開催枠として扱う。"""
    primary_coach = fixed_lesson.primary_coach()
    occurrence_court = occurrence_court or fixed_lesson.court
    candidates = list(
        CoachAvailability.objects.select_for_update()
        .filter(start_at=start_at, end_at=end_at)
        .filter(
            models.Q(fixed_lesson_source=fixed_lesson)
            | models.Q(lesson_type=fixed_lesson.lesson_type, coach=primary_coach)
        )
        .order_by(models.Case(
            models.When(fixed_lesson_source=fixed_lesson, then=0),
            default=1,
        ), "id")
    )

    if candidates:
        availability = candidates[0]
    else:
        availability = CoachAvailability(
            coach=primary_coach,
            coach_2=fixed_lesson.coach_2,
            court=occurrence_court,
            lesson_type=fixed_lesson.lesson_type,
            start_at=start_at,
            end_at=end_at,
            capacity=required_capacity,
            coach_count=fixed_lesson.coach_count,
            court_count=fixed_lesson.court_count,
            target_level=fixed_lesson.target_level,
            target_level_2=fixed_lesson.target_level_2,
            note=_fixed_note(fixed_lesson),
            fixed_lesson_source=fixed_lesson,
            coach_assignment_overridden=False,
            capacity_overridden=False,
            court_assignment_overridden=False,
            level_overridden=False,
            lesson_type_overridden=False,
            note_overridden=False,
        )
        availability.save()
        candidates = [availability]

    active_count = Reservation.objects.filter(
        availability=availability,
        status__in=CAPACITY_CONSUMING_STATUSES,
    ).count()
    if not availability.capacity_overridden and required_capacity < active_count:
        raise ValidationError(
            f"開催回の参加者{active_count}名を下回る定員{required_capacity}名には変更できません。"
        )
    if (
        not availability.lesson_type_overridden
        and availability.lesson_type != fixed_lesson.lesson_type
        and (
            Reservation.objects.filter(availability=availability).exists()
            or LessonWaitlist.objects.filter(
                availability=availability,
                status=LessonWaitlist.STATUS_WAITING,
            ).exists()
        )
    ):
        raise ValidationError(
            "予約またはキャンセル待ちがある未来開催回のレッスン種別は変更できません。"
        )

    desired_values = {}
    if not availability.capacity_overridden:
        desired_values["capacity"] = required_capacity
    if not availability.court_assignment_overridden:
        desired_values.update(court=occurrence_court, court_count=fixed_lesson.court_count)
    if not availability.level_overridden:
        desired_values.update(
            target_level=fixed_lesson.target_level,
            target_level_2=fixed_lesson.target_level_2,
        )
    if not availability.lesson_type_overridden:
        desired_values["lesson_type"] = fixed_lesson.lesson_type
    if not availability.note_overridden:
        desired_values["note"] = _fixed_note(fixed_lesson)
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

    reconcile_fixed_lesson_availability(availability, fixed_lesson)

    duplicate_ids = [item.pk for item in candidates[1:]]
    if duplicate_ids:
        Reservation.objects.filter(
            availability_id__in=duplicate_ids,
            fixed_lesson=fixed_lesson,
            start_at=start_at,
            end_at=end_at,
            status__in=CAPACITY_CONSUMING_STATUSES,
        ).update(
            coach=availability.coach,
            court=availability.court,
            availability=availability,
            lesson_type=availability.lesson_type,
            target_level=availability.target_level,
            target_level_2=availability.target_level_2,
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
            coach=availability.coach,
            court=availability.court,
            availability=availability,
            lesson_type=availability.lesson_type,
            target_level=availability.target_level,
            target_level_2=availability.target_level_2,
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


def restore_fixed_lesson_coach_assignment(availability_id):
    """Explicitly discard one occurrence's coach override."""
    with transaction.atomic():
        availability = (
            CoachAvailability.objects.select_for_update()
            .select_related("fixed_lesson_source")
            .get(pk=availability_id)
        )
        fixed_lesson = availability.fixed_lesson_source
        if fixed_lesson is None:
            raise ValidationError("固定レッスン由来ではない開催回です。")
        availability.coach = fixed_lesson.primary_coach()
        availability.coach_2 = fixed_lesson.coach_2
        availability.coach_count = fixed_lesson.coach_count
        availability.coach_assignment_overridden = False
        availability.save(update_fields=[
            "coach", "coach_2", "coach_count", "coach_assignment_overridden",
        ])
        return availability


def restore_fixed_lesson_occurrence_attributes(availability_id):
    """Discard occurrence attribute overrides and immediately restore the template values."""
    with transaction.atomic():
        availability = (
            CoachAvailability.objects.select_for_update()
            .select_related("fixed_lesson_source")
            .get(pk=availability_id)
        )
        fixed_lesson = availability.fixed_lesson_source
        if fixed_lesson is None:
            raise ValidationError("固定レッスン由来ではない開催回です。")
        for field_name in (
            "capacity_overridden", "court_assignment_overridden", "level_overridden",
            "lesson_type_overridden", "note_overridden",
        ):
            setattr(availability, field_name, False)
        availability.save(update_fields=[
            "capacity_overridden", "court_assignment_overridden", "level_overridden",
            "lesson_type_overridden", "note_overridden",
        ])
        required_capacity = max(fixed_lesson.effective_capacity(), 1)
        return _canonical_availability(
            fixed_lesson,
            availability.start_at,
            availability.end_at,
            required_capacity,
            occurrence_court=fixed_lesson.court,
        )


def _ensure_self_snapshot(reservation):
    participant = resolve_reservation_participant(reservation.user, PARTICIPANT_SELF)
    save_reservation_participant_snapshot(reservation, participant)


def rebind_occurrence_links(
    fixed_lesson, availability, start_at, end_at, *, apply=True,
    preserve_parallel=True, reservation_ids=None,
):
    """Bind one availability-backed occurrence to its current FixedLesson.

    Availability is the stable occurrence identity already stored on reservations.
    QuerySet.update deliberately avoids save signals, notifications, and ticket logic.
    """
    if (
        availability.start_at != start_at
        or availability.end_at != end_at
    ):
        raise ValidationError("開催枠と固定レッスンの日時が一致しません。")

    from .lesson_participants import competing_fixed_lesson_ids

    competing_ids = (
        competing_fixed_lesson_ids(fixed_lesson, start_at, end_at)
        if preserve_parallel
        else []
    )
    reservations = Reservation.objects.filter(
        availability=availability,
        lesson_type=availability.lesson_type,
        start_at=start_at,
        end_at=end_at,
    ).exclude(fixed_lesson=fixed_lesson).exclude(fixed_lesson_id__in=competing_ids)
    if reservation_ids is not None:
        reservations = reservations.filter(pk__in=reservation_ids)
    waitlists = LessonWaitlist.objects.filter(
        availability=availability,
        lesson_type=availability.lesson_type,
        start_at=start_at,
        end_at=end_at,
    ).exclude(fixed_lesson=fixed_lesson).exclude(fixed_lesson_id__in=competing_ids)
    if reservation_ids is not None:
        waitlists = waitlists.none()
    result = {
        "reservation_ids": list(reservations.values_list("id", flat=True)),
        "waitlist_ids": list(waitlists.values_list("id", flat=True)),
    }
    if apply:
        reservations.update(fixed_lesson=fixed_lesson)
        waitlists.update(fixed_lesson=fixed_lesson)
    return result


def _active_occurrence_reservations(fixed_lesson, member, availability, start_at, end_at):
    return list(
        Reservation.objects.select_for_update()
        .filter(
            user=member,
            start_at=start_at,
            end_at=end_at,
            status__in=CAPACITY_CONSUMING_STATUSES,
        )
        .filter(
            models.Q(participant_snapshot__participant_type="self")
            | models.Q(participant_snapshot__isnull=True)
        )
        .filter(
            models.Q(fixed_lesson=fixed_lesson)
            | models.Q(availability=availability)
            | models.Q(
                coach=availability.coach,
                court=availability.court,
                lesson_type=availability.lesson_type,
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
            coach=availability.coach,
            substitute_coach=availability.substitute_coach,
            court=availability.court,
            availability=availability,
            fixed_lesson=fixed_lesson,
            is_fixed_entry=True,
            lesson_type=availability.lesson_type,
            target_level=availability.target_level,
            target_level_2=availability.target_level_2,
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
            "coach": availability.coach,
            "substitute_coach": availability.substitute_coach,
            "court": availability.court,
            "availability": availability,
            "fixed_lesson": fixed_lesson,
            "is_fixed_entry": True,
            "lesson_type": availability.lesson_type,
            "target_level": availability.target_level,
            "target_level_2": availability.target_level_2,
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
        canceled_dates = set(
            fixed_lesson.canceled_occurrences.values_list("occurrence_date", flat=True)
        )
        target_dates = [
            target_date
            for target_date in _rolling_target_dates(fixed_lesson, today)
            if target_date not in canceled_dates
        ]
        target_datetimes = {
            fixed_lesson._build_datetimes_for_date(target_date)
            for target_date in target_dates
        }
        members = list(fixed_lesson.members.select_for_update().order_by("pk"))
        member_ids = {member.pk for member in members}
        required_capacity = max(fixed_lesson.effective_capacity(), 1)
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
                status__in=CAPACITY_CONSUMING_STATUSES,
            ).update(
                coach=availability.coach,
                court=availability.court,
                availability=availability,
                lesson_type=availability.lesson_type,
                target_level=availability.target_level,
                target_level_2=availability.target_level_2,
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
                coach=availability.coach,
                court=availability.court,
                availability=availability,
                lesson_type=availability.lesson_type,
                target_level=availability.target_level,
                target_level_2=availability.target_level_2,
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
                    start_at=start_at,
                    end_at=end_at,
                    status__in=CAPACITY_CONSUMING_STATUSES,
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
