from unittest.mock import patch

from django.db import transaction
from django.test import TestCase

from club.models import LineAccountLink, User
from club.notification_service import deliver_to_users, freeze_recipients, schedule_delivery


class NotificationServiceTests(TestCase):
    def setUp(self):
        self.first = User.objects.create_user(
            username="notify-first", email="SAME@example.com"
        )
        self.second = User.objects.create_user(
            username="notify-second", email="same@example.com"
        )
        LineAccountLink.objects.create(
            user=self.first, line_user_id="U-notify", is_active=True
        )

    @patch("club.notification_service.send_email_to_address", return_value=True)
    def test_duplicate_email_is_sent_once(self, send_email):
        result = deliver_to_users(
            [self.first, self.second], subject="subject", message="message"
        )
        send_email.assert_called_once_with("same@example.com", "subject", "message")
        self.assertEqual(result["email_sent"], 1)

    @patch("club.notification_service.send_email_to_address", return_value=True)
    @patch("club.notification_service.send_line_to_id", return_value=False)
    def test_line_failure_falls_back_to_email(self, send_line, send_email):
        result = deliver_to_users(
            [self.first], subject="subject", message="message",
            media=("line", "email"), email_fallback=True,
        )
        send_line.assert_called_once_with("U-notify", "message")
        send_email.assert_called_once()
        self.assertEqual(result, {"line_sent": 0, "email_sent": 1, "failed": 0, "skipped": 0})

    @patch("club.notification_service.send_email_to_address", return_value=True)
    def test_rollback_discards_delivery(self, send_email):
        recipients = freeze_recipients([self.first])
        try:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                with transaction.atomic():
                    schedule_delivery(
                        recipients, subject="subject", message="message"
                    )
                    raise RuntimeError("rollback")
        except RuntimeError:
            pass
        self.assertEqual(callbacks, [])
        send_email.assert_not_called()

    @patch("club.notification_service.send_email_to_address", return_value=True)
    def test_callback_uses_frozen_address_once(self, send_email):
        recipients = freeze_recipients([self.first])
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            schedule_delivery(recipients, subject="subject", message="message")
            self.first.email = "changed@example.com"
        self.assertEqual(len(callbacks), 1)
        send_email.assert_called_once_with("same@example.com", "subject", "message")
