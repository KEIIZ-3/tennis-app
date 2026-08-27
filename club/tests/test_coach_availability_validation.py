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
        self.court = Court.objects.create(
            name="Availability validation court", available_court_count=12
        )
        self.other_court = Court.objects.create(name="Availability validation other court")
        self.start_at = timezone.make_aware(datetime(2026, 8, 24, 10))

    def create_availability(
        self, *, coach=None, court=None, start_at=None, end_at=None,
        target_level=User.LEVEL_BEGINNER, is_recruitment_closed=False,
    ):
        start_at = start_at or self.start_at
        return CoachAvailability.objects.create(
            coach=coach or self.coach,
            court=court or self.court,
            lesson_type=CoachAvailability.LESSON_PRIVATE,
            target_level=target_level,
            start_at=start_at,
            end_at=end_at or start_at + timedelta(hours=1),
            capacity=1,
            is_recruitment_closed=is_recruitment_closed,
        )

    def set_court_count(self, availability, court_count):
        CoachAvailability.objects.filter(pk=availability.pk).update(court_count=court_count)
        availability.refresh_from_db()
        return availability

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

    def test_different_coach_can_share_court_within_capacity(self):
        self.create_availability()
        self.assertIsNotNone(self.create_availability(coach=self.other_coach).pk)

    def test_different_level_can_share_court_within_capacity(self):
        self.create_availability()
        availability = self.create_availability(
            coach=self.other_coach, target_level=User.LEVEL_INTERMEDIATE
        )
        self.assertIsNotNone(availability.pk)

    def test_total_equal_to_court_capacity_is_allowed(self):
        self.set_court_count(self.create_availability(), 11)
        self.assertIsNotNone(self.create_availability(coach=self.other_coach).pk)

    def test_total_over_court_capacity_is_rejected_with_usage_details(self):
        self.set_court_count(self.create_availability(), 11)
        third_coach = User.objects.create_user(
            username="availability-overlap-third-coach", role=User.ROLE_COACH
        )

        with self.assertRaisesMessage(
            ValidationError, "利用中 11面 / 追加 2面 / 利用可能 12面"
        ):
            CoachAvailability.objects.create(
                coach=self.other_coach, coach_2=third_coach, court=self.court,
                lesson_type=CoachAvailability.LESSON_GENERAL,
                target_level=User.LEVEL_BEGINNER,
                start_at=self.start_at, end_at=self.start_at + timedelta(hours=2),
            )

    def test_same_coach_can_use_non_overlapping_time(self):
        self.create_availability()

        availability = self.create_availability(
            start_at=self.start_at + timedelta(hours=1)
        )

        self.assertIsNotNone(availability.pk)

    def test_edit_does_not_treat_itself_as_overlap(self):
        availability = self.create_availability()
        self.court.available_court_count = 1
        self.court.save(update_fields=["available_court_count"])
        availability.target_level_2 = availability.target_level

        availability.save(update_fields=["target_level_2"])

        availability.refresh_from_db()
        self.assertEqual(availability.target_level_2, "")

    def test_non_overlapping_availability_does_not_consume_court_capacity(self):
        self.set_court_count(self.create_availability(), 12)
        availability = self.create_availability(
            coach=self.other_coach, start_at=self.start_at + timedelta(hours=1)
        )
        self.assertIsNotNone(availability.pk)

    def test_recruitment_closed_availability_still_consumes_court_capacity(self):
        self.set_court_count(self.create_availability(is_recruitment_closed=True), 12)
        with self.assertRaisesMessage(ValidationError, "利用可能コート面数を超えています"):
            self.create_availability(coach=self.other_coach)

    def test_all_availability_statuses_consume_court_capacity(self):
        statuses = (
            CoachAvailability.STATUS_OPEN,
            CoachAvailability.STATUS_REQUESTED,
            CoachAvailability.STATUS_APPROVED,
        )
        for index, status in enumerate(statuses):
            start_at = self.start_at + timedelta(days=index)
            existing = self.create_availability(start_at=start_at)
            CoachAvailability.objects.filter(pk=existing.pk).update(
                court_count=12, status=status
            )
            with self.assertRaisesMessage(ValidationError, "利用可能コート面数を超えています"):
                self.create_availability(coach=self.other_coach, start_at=start_at)
