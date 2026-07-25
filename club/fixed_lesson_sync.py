from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone

from .models import FixedLesson, Reservation


@dataclass(frozen=True)
class FixedLessonSyncResult:
    changed_count: int
    missing_count: int
    missing_details: tuple[str, ...]

    @property
    def is_complete(self):
        return self.missing_count == 0


def _protected_occurrence_exists(fixed_lesson, member, start_at, end_at):
    return Reservation.objects.filter(
        user=member,
        fixed_lesson=fixed_lesson,
        is_fixed_entry=True,
        start_at=start_at,
        end_at=end_at,
    ).filter(
        Q(status=Reservation.STATUS_RAIN_CANCELED)
        | Q(
            status=Reservation.STATUS_CANCELED,
            cancellation_reason="会員が予約確認画面からキャンセル",
        )
    ).exists()


def _active_reservation_exists(fixed_lesson, member, start_at, end_at):
    return Reservation.objects.filter(
        user=member,
        fixed_lesson=fixed_lesson,
        is_fixed_entry=True,
        start_at=start_at,
        end_at=end_at,
        status__in=[Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING],
    ).exists()


def validate_fixed_member_reservations(fixed_lesson):
    if not fixed_lesson.is_active or not fixed_lesson.court_id:
        return ()

    today = timezone.localdate()
    missing_details = []
    members = list(fixed_lesson.members.all().order_by("pk"))

    for target_date in fixed_lesson.scheduled_occurrence_dates():
        if target_date < today:
            continue

        start_at, end_at = fixed_lesson._build_datetimes_for_date(target_date)
        for member in members:
            if _active_reservation_exists(
                fixed_lesson,
                member,
                start_at,
                end_at,
            ):
                continue
            if _protected_occurrence_exists(
                fixed_lesson,
                member,
                start_at,
                end_at,
            ):
                continue

            missing_details.append(
                f"{target_date:%Y-%m-%d} / {member.display_name()}"
            )

    return tuple(missing_details)


def sync_and_validate_fixed_lesson(fixed_lesson, created_by=None):
    changed_count = fixed_lesson.sync_future_reservations(created_by=created_by)
    missing_details = validate_fixed_member_reservations(fixed_lesson)

    if missing_details:
        preview = "、".join(missing_details[:5])
        suffix = "" if len(missing_details) <= 5 else f" ほか{len(missing_details) - 5}件"
        raise ValidationError(
            "固定メンバーの予約生成が完了していません。"
            f"不足: {preview}{suffix}。"
            "対象日時の予約重複、コーチスケジュール、締め済み月、"
            "または予約入力条件を確認してください。"
        )

    return FixedLessonSyncResult(
        changed_count=changed_count,
        missing_count=0,
        missing_details=(),
    )
