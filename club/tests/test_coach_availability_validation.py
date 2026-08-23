from datetime import datetime, timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from club.models import CoachAvailability, Court, User


class CoachAvailabilityOverlapValidationTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(
            username="availability-overlap-coach", role=User.ROLE_COACH
        )
        self.other_coach = User.objects.create_user(
            username="availability-overlap-other-coach", role=User.ROLE_COACH
        )
        self.court = Court.objects.create(name="Availability validation court")
        self.other_court = Court.objects.create(name="Availability validation other court")
        self.start_at = timezone.make_aware(datetime(2026, 8, 24, 10))

    def create_availability(self, *, coach=None, court=None, start_at=None, end_at=None):
        start_at = start_at or self.start_at
        return CoachAvailability.objects.create(
            coach=coach or self.coach,
            court=court or self.court,
            lesson_type=CoachAvailability.LESSON_PRIVATE,
            target_level=User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=end_at or start_at + timedelta(hours=1),
            capacity=1,
        )

    def test_same_coach_overlapping_time_raises_validation_error(self):
        self.create_availability()

        with self.assertRaisesMessage(
            ValidationError,
            "同じコーチで重複する空き時間があります。",
        ):
            self.create_availability(
                start_at=self.start_at,
                end_at=self.start_at + timedelta(hours=2),
            )

    def test_different_coach_and_court_can_use_same_time(self):
        self.create_availability()

        availability = self.create_availability(coach=self.other_coach, court=self.other_court)

        self.assertIsNotNone(availability.pk)

    def test_same_coach_can_use_non_overlapping_time(self):
        self.create_availability()

        availability = self.create_availability(
            start_at=self.start_at + timedelta(hours=1)
        )

        self.assertIsNotNone(availability.pk)

    def test_edit_does_not_treat_itself_as_overlap(self):
        availability = self.create_availability()
        availability.target_level_2 = availability.target_level

        availability.save(update_fields=["target_level_2"])

        availability.refresh_from_db()
        self.assertEqual(availability.target_level_2, "")
