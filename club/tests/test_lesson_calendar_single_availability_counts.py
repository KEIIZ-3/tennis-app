from datetime import datetime

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.models import CoachAvailability, Court, Reservation, User


class LessonCalendarSingleAvailabilityCountTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(
            username="single-count-coach",
            role=User.ROLE_COACH,
        )
        self.other_coach = User.objects.create_user(
            username="single-count-other-coach",
            role=User.ROLE_COACH,
        )
        self.court = Court.objects.create(name="single-count-court")
        self.other_court = Court.objects.create(name="single-count-other-court")

    def _aware(self, day, hour):
        return timezone.make_aware(datetime(2026, 9, day, hour))

    def _availability(self, *, day, capacity, coach=None, coach_2=None, court=None):
        return CoachAvailability.objects.create(
            coach=coach or self.coach,
            coach_2=coach_2,
            court=court or self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_at=self._aware(day, 19),
            end_at=self._aware(day, 21),
            capacity=capacity,
        )

    def _reservations(self, availability, statuses):
        reservations = []
        for index, status in enumerate(statuses):
            member = User.objects.create_user(
                username=f"single-count-member-{availability.pk}-{index}",
                role=User.ROLE_MEMBER,
            )
            reservations.append(
                Reservation(
                    user=member,
                    coach=availability.coach,
                    court=availability.court,
                    availability=availability,
                    lesson_type=availability.lesson_type,
                    target_level=availability.target_level,
                    start_at=availability.start_at,
                    end_at=availability.end_at,
                    status=status,
                )
            )
        Reservation.objects.bulk_create(reservations)

    def _calendar_item(self, availability):
        response = self.client.get(
            reverse("club:lesson_calendar"),
            {"year": 2026, "month": 9},
        )
        self.assertEqual(response.status_code, 200)
        return next(
            item
            for week in response.context["calendar_weeks"]
            for day in week
            for item in day["items"]
            if item["availability_id"] == str(availability.pk)
        )

    def test_september_4_active_reservations_reach_final_calendar_context(self):
        availability = self._availability(
            day=4,
            capacity=10,
            coach_2=self.other_coach,
        )
        self._reservations(availability, [Reservation.STATUS_ACTIVE] * 7)

        item = self._calendar_item(availability)

        self.assertEqual(item["member_count"], 7)
        self.assertEqual(item["pending_count"], 0)
        self.assertEqual(item["capacity_consuming_count"], 7)
        self.assertEqual(item["capacity"], 10)
        self.assertEqual(item["remaining_count"], 3)

    def test_september_11_statuses_and_same_time_slots_stay_separate(self):
        availability = self._availability(day=11, capacity=5)
        other_availability = self._availability(
            day=11,
            capacity=8,
            coach=self.other_coach,
            court=self.other_court,
        )
        self._reservations(
            availability,
            [
                Reservation.STATUS_ACTIVE,
                Reservation.STATUS_ACTIVE,
                Reservation.STATUS_ACTIVE,
                Reservation.STATUS_PENDING,
                Reservation.STATUS_CANCELED,
            ],
        )
        self._reservations(other_availability, [Reservation.STATUS_ACTIVE] * 2)

        item = self._calendar_item(availability)

        self.assertEqual(item["member_count"], 3)
        self.assertEqual(item["pending_count"], 1)
        self.assertEqual(item["capacity_consuming_count"], 4)
        self.assertEqual(item["remaining_count"], 1)
