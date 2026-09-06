from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from club.models import CoachAvailability, Court, LineAccountLink, Reservation
from club.reservation_notification_service import (
    schedule_occurrence_rain_canceled_notifications,
)


class RainCancellationNotificationTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.coach = user_model.objects.create_user(
            username="rain-coach", role=user_model.ROLE_COACH
        )
        self.first = user_model.objects.create_user(username="rain-first")
        self.second = user_model.objects.create_user(username="rain-second")
        self.court = Court.objects.create(name="Rain notification court")
        self.start_at = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=1), time(10))
        )
        self.availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=user_model.LEVEL_BEGINNER,
            start_at=self.start_at,
            end_at=self.start_at + timedelta(hours=1),
            capacity=6,
        )
        LineAccountLink.objects.create(
            user=self.first, line_user_id="U-rain-first", is_active=True
        )
        LineAccountLink.objects.create(
            user=self.second, line_user_id="U-rain-second", is_active=True
        )

    def reservation(self, user, *, status=Reservation.STATUS_RAIN_CANCELED, **kwargs):
        return Reservation.objects.create(
            user=user,
            coach=self.coach,
            court=self.court,
            availability=self.availability,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=get_user_model().LEVEL_BEGINNER,
            start_at=self.start_at,
            end_at=self.start_at + timedelta(hours=1),
            status=status,
            **kwargs,
        )

    @patch("club.notification_service.send_line_to_id", return_value=True)
    def test_notifies_each_linked_participant_after_commit(self, send_line):
        first = self.reservation(self.first)
        second = self.reservation(self.second)

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            schedule_occurrence_rain_canceled_notifications([first.pk, second.pk])
            send_line.assert_not_called()

        self.assertEqual(len(callbacks), 1)
        self.assertEqual(
            {call.args[0] for call in send_line.call_args_list},
            {"U-rain-first", "U-rain-second"},
        )

    @patch("club.notification_service.send_line_to_id", return_value=True)
    def test_duplicate_reservations_for_same_account_send_once(self, send_line):
        first = self.reservation(self.first)
        duplicate = self.reservation(self.first)

        with self.captureOnCommitCallbacks(execute=True):
            schedule_occurrence_rain_canceled_notifications(
                [first.pk, duplicate.pk, first.pk]
            )

        send_line.assert_called_once()

    @patch("club.notification_service.send_line_to_id", return_value=True)
    def test_skips_non_rain_canceled_guest_and_unlinked_reservations(self, send_line):
        canceled = self.reservation(self.first, status=Reservation.STATUS_CANCELED)
        guest = self.reservation(None)
        unlinked = self.reservation(
            get_user_model().objects.create_user(username="rain-unlinked")
        )

        with self.captureOnCommitCallbacks(execute=True):
            schedule_occurrence_rain_canceled_notifications(
                [canceled.pk, guest.pk, unlinked.pk]
            )

        send_line.assert_not_called()

    @patch("club.notification_service.send_line_to_id", return_value=False)
    def test_line_failure_does_not_change_completed_cancellation(self, send_line):
        reservation = self.reservation(self.first)

        with self.captureOnCommitCallbacks(execute=True):
            schedule_occurrence_rain_canceled_notifications([reservation.pk])

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.STATUS_RAIN_CANCELED)
        send_line.assert_called_once()

    @patch("club.notification_service.send_line_to_id", return_value=True)
    def test_rollback_discards_notification(self, send_line):
        reservation = self.reservation(self.first)

        try:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                with transaction.atomic():
                    schedule_occurrence_rain_canceled_notifications([reservation.pk])
                    raise RuntimeError("rollback")
        except RuntimeError:
            pass

        self.assertEqual(callbacks, [])
        send_line.assert_not_called()

    @patch("club.notification_service.send_line_to_id", return_value=True)
    def test_repost_snapshot_without_eligible_reservations_queues_nothing(self, send_line):
        self.reservation(self.first)

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            result = schedule_occurrence_rain_canceled_notifications([])

        self.assertEqual(result, {"queued": 0})
        self.assertEqual(callbacks, [])
        send_line.assert_not_called()
