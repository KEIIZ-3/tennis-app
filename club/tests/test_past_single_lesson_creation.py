from datetime import date, datetime, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.models import CoachAvailability, Court, User


class PastSingleLessonCreationTests(TestCase):
    today = date(2026, 8, 22)

    def setUp(self):
        self.coach = User.objects.create_user(
            username="single-lesson-coach",
            password="password",
            role="coach",
        )
        self.court = Court.objects.create(name="単発レッスンテストコート")
        self.client.force_login(self.coach)

    def _aware(self, target_date, hour):
        return timezone.make_aware(datetime.combine(target_date, datetime.min.time()).replace(hour=hour))

    def _post_data(self, target_date):
        return {
            "coach": self.coach.pk,
            "court": self.court.pk,
            "lesson_type": "general",
            "target_level": "beginner",
            "coach_count": 1,
            "court_count": 1,
            "capacity": 6,
            "custom_ticket_price": 0,
            "custom_duration_hours": 0,
            "note": "",
            "start_date": target_date.isoformat(),
            "start_hour": "9",
            "end_date": target_date.isoformat(),
            "end_hour": "11",
        }

    @patch("club.views.timezone.localdate", return_value=today)
    def test_calendar_only_shows_creation_link_for_today_and_future(self, _localdate):
        response = self.client.get(
            reverse("club:lesson_calendar"),
            {"year": self.today.year, "month": self.today.month},
        )

        self.assertNotContains(response, f"?date={(self.today - timedelta(days=1)).isoformat()}")
        self.assertContains(response, f"?date={self.today.isoformat()}")
        self.assertContains(response, f"?date={(self.today + timedelta(days=1)).isoformat()}")

    @patch("club.views.timezone.localdate", return_value=today)
    def test_past_date_get_parameter_is_rejected(self, _localdate):
        response = self.client.get(
            reverse("club:coach_availability_create"),
            {"date": (self.today - timedelta(days=1)).isoformat()},
        )

        self.assertEqual(response.status_code, 400)

    @patch("club.forms.timezone.localdate", return_value=today)
    def test_past_date_post_is_not_saved(self, _localdate):
        response = self.client.post(
            reverse("club:coach_availability_create"),
            self._post_data(self.today - timedelta(days=1)),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "過去の日付には新しい単発レッスンを登録できません。")
        self.assertFalse(CoachAvailability.objects.exists())

    @patch("club.forms.timezone.localdate", return_value=today)
    def test_existing_past_availability_can_still_be_edited(self, _localdate):
        past_date = self.today - timedelta(days=1)
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type="general",
            target_level="beginner",
            start_at=self._aware(past_date, 9),
            end_at=self._aware(past_date, 11),
            coach_count=1,
            court_count=1,
            capacity=6,
        )
        data = self._post_data(past_date)
        data["note"] = "事後修正"

        response = self.client.post(
            reverse("club:coach_availability_edit", args=[availability.pk]),
            data,
        )

        self.assertRedirects(response, reverse("club:coach_availability_list"))
        availability.refresh_from_db()
        self.assertEqual(availability.note, "事後修正")

    @patch("club.views.timezone.localdate", return_value=today)
    def test_existing_past_lesson_card_remains_linked(self, _localdate):
        past_date = self.today - timedelta(days=1)
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type="event",
            target_level="beginner",
            start_at=self._aware(past_date, 9),
            end_at=self._aware(past_date, 10),
            coach_count=1,
            court_count=1,
            capacity=6,
            custom_duration_hours=1,
        )

        response = self.client.get(
            reverse("club:lesson_calendar"),
            {"year": past_date.year, "month": past_date.month},
        )

        self.assertContains(response, f'availability_id={availability.pk}')
        self.assertContains(response, 'class="calendar-event')
