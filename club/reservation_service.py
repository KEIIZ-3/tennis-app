from django.core.exceptions import ValidationError

from .models import Reservation


def create_reservation(**values):
    """Create every reservation through the model's canonical validation path."""
    reservation = Reservation(**values)
    reservation.save()
    return reservation


def change_reservation_status(reservation_id, *, target_status, created_by=None):
    """Apply an existing canonical Reservation state transition."""
    reservation = Reservation.objects.get(pk=reservation_id)
    if reservation.status == target_status:
        return reservation

    if target_status == Reservation.STATUS_CANCELED:
        reservation.cancel(
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
