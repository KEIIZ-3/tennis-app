import logging

from django.db import transaction

from .models import Reservation
from .notifications import build_reservation_canceled_message, notify_user_email_only


logger = logging.getLogger(__name__)


def _send_reservation_canceled_notification(reservation_id):
    """Send from committed database state; notification failures never undo the save."""
    try:
        reservation = Reservation.objects.select_related("user").get(pk=reservation_id)
        if reservation.status != Reservation.STATUS_CANCELED:
            return False
        message = build_reservation_canceled_message(reservation)
        notify_user_email_only(
            reservation.user,
            message,
            subject="【Play Design Tennis】予約キャンセル通知",
        )
        return True
    except Reservation.DoesNotExist:
        return False
    except Exception as exc:
        logger.warning("reservation canceled notification failed: %s", exc)
        return False


def schedule_reservation_canceled_notification(reservation_id):
    """Schedule exactly one cancellation email for this completed business operation."""
    frozen_reservation_id = int(reservation_id)
    transaction.on_commit(
        lambda reservation_id=frozen_reservation_id: _send_reservation_canceled_notification(
            reservation_id
        )
    )
