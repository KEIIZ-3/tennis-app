import logging

from django.db import transaction

from .models import Reservation
from .notification_service import deliver, freeze_recipients
from .notifications import build_reservation_canceled_message


logger = logging.getLogger(__name__)


def _reservation_canceled_payload(reservation_id):
    try:
        reservation = Reservation.objects.select_related("user").get(pk=reservation_id)
        if reservation.status != Reservation.STATUS_CANCELED:
            return None
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

    def send_after_commit():
        try:
            payload = _reservation_canceled_payload(frozen_reservation_id)
            if payload is None:
                return {"line_sent": 0, "email_sent": 0, "failed": 0, "skipped": 0}
            recipients, subject, message = payload
            return deliver(recipients, subject=subject, message=message, media=("email",))
        except Exception:
            logger.exception("reservation canceled notification delivery failed")
            return {"line_sent": 0, "email_sent": 0, "failed": 1, "skipped": 0}

    transaction.on_commit(send_after_commit)
    return {"queued": 1}
