from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Reservation


def create_reservation(**values):
    """Create every reservation through the model's canonical validation path."""
    reservation = Reservation(**values)
    reservation.save()
    if reservation.availability_id and reservation.fixed_lesson_id:
        from .fixed_lesson_occurrence_service import reconcile_fixed_lesson_availability

        reconcile_fixed_lesson_availability(
            reservation.availability,
            reservation.fixed_lesson,
        )
    return reservation


@transaction.atomic
def cancel_reservation_with_settlement(
    reservation_id, *, created_by=None, reason="", schedule_notification=True
):
    """Cancel a reservation and refresh its canonical settlement in one transaction."""
    reservation = Reservation.objects.select_for_update().get(pk=reservation_id)
    changed = reservation.cancel(
        created_by=created_by,
        reason=reason,
        schedule_notification=schedule_notification,
    )
    if changed:
        from .settlement_service import calculate_monthly_settlement

        calculate_monthly_settlement(
            reservation.start_at.year,
            reservation.start_at.month,
            force=True,
        )
    return reservation, changed


def change_reservation_status(reservation_id, *, target_status, created_by=None):
    """Apply an existing canonical Reservation state transition."""
    reservation = Reservation.objects.get(pk=reservation_id)
    if reservation.status == target_status:
        return reservation

    if target_status == Reservation.STATUS_CANCELED:
        reservation, _changed = cancel_reservation_with_settlement(
            reservation.pk,
            created_by=created_by,
            reason="管理画面からキャンセル",
        )
        return reservation

    if (
        reservation.status == Reservation.STATUS_PENDING
        and target_status == Reservation.STATUS_ACTIVE
    ):
        reservation.activate_after_approval(created_by=created_by)
        return reservation

    raise ValidationError("管理画面からはこの予約状態へ変更できません。")
