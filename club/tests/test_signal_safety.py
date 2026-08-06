import importlib
from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.apps import apps
from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.test import TestCase
from django.utils import timezone

from club.apps import ClubConfig
from club.models import CoachAvailability, Court, Reservation, User
from club.reservation_notification_service import schedule_reservation_canceled_notification


class SignalSafetyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="signal-member",
            password="test-password",
            email="signal@example.com",
            is_profile_completed=True,
        )
        self.coach = User.objects.create_user(
            username="signal-coach",
            password="test-password",
            role=User.ROLE_COACH,
        )
        self.court = Court.objects.create(name="signal-test-court")
        start_at = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=2), time(10, 0))
        )
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            start_at=start_at,
            end_at=start_at + timedelta(hours=2),
            capacity=6,
        )
        self.reservation = Reservation.objects.create(
            user=self.user,
            coach=self.coach,
            court=self.court,
            availability=availability,
            lesson_type=Reservation.LESSON_GENERAL,
            start_at=start_at,
            end_at=start_at + timedelta(hours=2),
            status=Reservation.STATUS_ACTIVE,
        )

    def test_ready_and_reload_keep_receivers_single(self):
        import club.signals as signals

        config = ClubConfig("club", importlib.import_module("club"))
        config.apps = apps
        config.ready()
        config.ready()
        importlib.reload(signals)

        self.assertTrue(pre_save.has_listeners(Reservation))
        self.assertTrue(post_save.has_listeners(Reservation))
        matching = [
            receiver
            for receiver in post_save._live_receivers(Reservation)[0]
            if receiver.__name__ == "reservation_status_notification"
        ]
        self.assertEqual(len(matching), 1)

    @patch("club.notification_service.send_email_to_address")
    def test_cancel_notifies_once_after_commit(self, notify_mock):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            self.assertTrue(self.reservation.cancel(created_by=self.user))

        self.assertEqual(len(callbacks), 1)
        notify_mock.assert_called_once()

    @patch("club.notification_service.send_email_to_address")
    def test_rollback_does_not_notify(self, notify_mock):
        try:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                with transaction.atomic():
                    self.reservation.status = Reservation.STATUS_CANCELED
                    self.reservation.save(update_fields=["status"])
                    raise RuntimeError("rollback")
        except RuntimeError:
            pass

        self.assertEqual(callbacks, [])
        notify_mock.assert_not_called()

    @patch("club.notification_service.send_email_to_address")
    def test_non_status_update_does_not_notify(self, notify_mock):
        self.reservation.cancellation_reason = "metadata only"
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            self.reservation.save(update_fields=["cancellation_reason"])

        self.assertEqual(callbacks, [])
        notify_mock.assert_not_called()

    @patch("club.notification_service.send_email_to_address")
    def test_non_canceled_reservation_is_skipped_after_commit(self, notify_mock):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            schedule_reservation_canceled_notification(self.reservation.pk)

        self.assertEqual(len(callbacks), 1)
        notify_mock.assert_not_called()

    @patch("club.notification_service.send_email_to_address")
    def test_status_restored_before_commit_is_not_notified(self, notify_mock):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with transaction.atomic():
                self.reservation.status = Reservation.STATUS_CANCELED
                self.reservation.save(update_fields=["status"])
                self.reservation.status = Reservation.STATUS_ACTIVE
                self.reservation.save(update_fields=["status"])

        self.assertEqual(len(callbacks), 1)
        notify_mock.assert_not_called()

    @patch("club.notification_service.send_email_to_address")
    def test_deleted_reservation_is_skipped_after_commit(self, notify_mock):
        reservation_id = self.reservation.pk
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with transaction.atomic():
                schedule_reservation_canceled_notification(reservation_id)
                self.reservation.delete()

        self.assertEqual(len(callbacks), 1)
        notify_mock.assert_not_called()
