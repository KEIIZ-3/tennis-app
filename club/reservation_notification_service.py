import logging

from .models import Reservation
from .notification_service import freeze_recipients, schedule_delivery
from .notifications import build_reservation_canceled_message


logger = logging.getLogger(__name__)


def _reservation_canceled_payload(reservation_id):
    try:
        reservation = Reservation.objects.select_related("user").get(pk=reservation_id)
        if reservation.status != Reservation.STATUS_CANCELED:
            return False
        return (
            freeze_recipients([reservation.user]),
            "【Play Design Tennis】予約キャンセル通知",
            build_reservation_canceled_message(reservation),
        )
    except Reservation.DoesNotExist:
        return None
    except Exception as exc:
        logger.warning("reservation canceled notification failed: %s", exc)
        return None


def schedule_reservation_canceled_notification(reservation_id):
    """Schedule exactly one cancellation email for this completed business operation."""
    frozen_reservation_id = int(reservation_id)
    payload = _reservation_canceled_payload(frozen_reservation_id)
    if payload is None:
        return {"queued": 0}
    recipients, subject, message = payload
    return schedule_delivery(recipients, subject=subject, message=message, media=("email",))
