from django.core.exceptions import ValidationError
from django.db import transaction


def _effective_capacity(reservation):
    availability = None
    if getattr(reservation, "availability_id", None):
        from .models import CoachAvailability

        availability = (
            CoachAvailability.objects.select_for_update()
            .filter(pk=reservation.availability_id)
            .first()
        )

    if availability is None:
        try:
            availability = reservation.matching_availability()
        except Exception:
            availability = None

        if availability is not None:
            from .models import CoachAvailability

            availability = (
                CoachAvailability.objects.select_for_update()
                .filter(pk=availability.pk)
                .first()
            )

    if availability is not None:
        try:
            return max(
                int(availability.effective_capacity()),
                int(availability.capacity or 0),
                1,
            )
        except Exception:
            return max(int(getattr(availability, "capacity", 1) or 1), 1)

    fixed_lesson = None
    if getattr(reservation, "fixed_lesson_id", None):
        from .models import FixedLesson

        fixed_lesson = (
            FixedLesson.objects.select_for_update()
            .filter(pk=reservation.fixed_lesson_id)
            .first()
        )

    if fixed_lesson is not None:
        try:
            return max(
                int(fixed_lesson.effective_capacity()),
                int(fixed_lesson.capacity or 0),
                1,
            )
        except Exception:
            return max(int(getattr(fixed_lesson, "capacity", 1) or 1), 1)

    return 1


def _fixed_member_count(reservation):
    if not getattr(reservation, "fixed_lesson_id", None):
        return 0

    try:
        return int(reservation.fixed_lesson.members.count())
    except Exception:
        try:
            from .models import FixedLesson

            fixed_lesson = FixedLesson.objects.filter(
                pk=reservation.fixed_lesson_id
            ).first()
            if fixed_lesson is None:
                return 0
            return int(fixed_lesson.members.count())
        except Exception:
            return 0


def _active_count_excluding_self(reservation):
    from .models import Reservation

    queryset = Reservation.objects.filter(
        coach_id=reservation.coach_id,
        court_id=reservation.court_id,
        lesson_type=reservation.lesson_type,
        start_at=reservation.start_at,
        end_at=reservation.end_at,
        status=Reservation.STATUS_ACTIVE,
    )

    if getattr(reservation, "pk", None):
        queryset = queryset.exclude(pk=reservation.pk)

    return queryset.count()


def _is_new_activation(reservation):
    from .models import Reservation

    if reservation.status != Reservation.STATUS_ACTIVE:
        return False

    if not getattr(reservation, "pk", None):
        return True

    previous_status = (
        Reservation.objects.filter(pk=reservation.pk)
        .values_list("status", flat=True)
        .first()
    )
    return previous_status != Reservation.STATUS_ACTIVE


def _validate_capacity_before_activation(reservation):
    if not _is_new_activation(reservation):
        return

    capacity = _effective_capacity(reservation)
    active_count = _active_count_excluding_self(reservation)
    fixed_member_count = _fixed_member_count(reservation)
    current_count = max(int(active_count or 0), int(fixed_member_count or 0))

    if current_count >= capacity:
        raise ValidationError(
            f"このレッスンは満員です（定員{capacity}名）。"
            "キャンセル待ちをご利用ください。"
        )


def apply_reservation_capacity_guard():
    from .models import Reservation

    if getattr(Reservation.save, "_capacity_guard_applied", False):
        return

    original_save = Reservation.save

    def guarded_save(self, *args, **kwargs):
        with transaction.atomic():
            _validate_capacity_before_activation(self)
            return original_save(self, *args, **kwargs)

    guarded_save._capacity_guard_applied = True
    guarded_save._original_save = original_save
    Reservation.save = guarded_save
