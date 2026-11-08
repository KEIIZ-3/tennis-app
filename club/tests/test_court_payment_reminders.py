from datetime import datetime, timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from club.court_payment_reminders import send_reminders, unpaid_availabilities
from club.models import (
    MAIN_COACH_NAMES,
    CoachAvailability,
    Court,
    CourtBookingInfo,
    CourtPaymentReminderLog,
    Reservation,
    User,
)


class CourtPaymentReminderTests(TestCase):
    def setUp(self):
        self.coaches = [
            User.objects.create_user(
                username=f"reminder-coach-{index}", role=User.ROLE_COACH, full_name=name
            )
            for index, name in enumerate(MAIN_COACH_NAMES)
        ]
        self.court = Court.objects.create(name="西猪名公園", is_active=True)
        self.target_date = timezone.localdate() + timedelta(days=8)
        start = timezone.make_aware(datetime.combine(self.target_date, datetime.min.time()).replace(hour=17))
        self.availability = CoachAvailability.objects.create(
            coach=self.coaches[0], court=self.court,
            start_at=start, end_at=start + timedelta(hours=2),
            court_fee_amount=3200, court_fee_overridden=True,
            court_payment_status=CoachAvailability.COURT_PAYMENT_UNPAID,
        )
        CourtBookingInfo.objects.create(
            availability=self.availability, position=1,
            reservation_number="0123456789", booking_account_kind="coach",
            booking_account_coach=self.coaches[0],
        )
        CourtBookingInfo.objects.create(
            availability=self.availability, position=2,
            reservation_number="9876543210", booking_account_kind="coach",
            booking_account_coach=self.coaches[1],
        )

    def test_filters_zero_unset_and_other_dates(self):
        self.assertEqual(unpaid_availabilities(self.target_date), [self.availability])
        self.availability.court_fee_amount = 0
        self.availability.save(update_fields=["court_fee_amount"])
        self.assertEqual(unpaid_availabilities(self.target_date), [])
        self.availability.court_fee_amount = 3200
        self.availability.court_payment_status = ""
        self.availability.save(update_fields=["court_fee_amount", "court_payment_status"])
        self.assertEqual(unpaid_availabilities(self.target_date), [])

    @patch("club.court_payment_reminders.notify_user_line_only")
    def test_aggregates_sends_once_per_coach_and_is_idempotent(self, notify):
        notify.return_value = {"line": True, "email": False}
        rows, recipients, message, results = send_reminders(self.target_date)
        self.assertEqual(rows, [self.availability])
        self.assertEqual(recipients, self.coaches)
        self.assertIn("0123456789 / 9876543210", message)
        self.assertEqual(notify.call_count, 3)
        self.assertEqual(CourtPaymentReminderLog.objects.count(), 3)
        send_reminders(self.target_date)
        self.assertEqual(notify.call_count, 3)

    @patch("club.court_payment_reminders.notify_user_line_only")
    def test_failed_recipient_is_retried_and_dry_run_never_sends(self, notify):
        notify.side_effect = [
            {"line": True, "email": False},
            {"line": False, "email": False},
            {"line": True, "email": False},
        ]
        send_reminders(self.target_date)
        self.assertEqual(CourtPaymentReminderLog.objects.count(), 2)
        notify.side_effect = None
        notify.return_value = {"line": True, "email": False}
        send_reminders(self.target_date)
        self.assertEqual(notify.call_count, 4)
        self.assertEqual(CourtPaymentReminderLog.objects.count(), 3)
        send_reminders(self.target_date, dry_run=True)
        self.assertEqual(notify.call_count, 4)

    @patch("club.court_payment_reminders.notify_user_line_only")
    def test_command_dry_run(self, notify):
        call_command(
            "send_court_payment_reminders",
            target_date=self.target_date.isoformat(),
            dry_run=True,
        )
        notify.assert_not_called()
