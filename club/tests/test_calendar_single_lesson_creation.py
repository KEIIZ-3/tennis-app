from datetime import date, datetime, timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.models import CoachAvailability, Court, FixedLesson, Reservation, User


class CalendarSingleLessonCreationTests(TestCase):
    today = date(2026, 8, 22)

    def setUp(self):
        self.coach = User.objects.create_user(
            username="calendar-single-coach", password="password", role=User.ROLE_COACH
        )
        self.contractor = User.objects.create_user(
            username="calendar-single-contractor",
            password="password",
            role=User.ROLE_CONTRACTOR_COACH,
        )
        self.member = User.objects.create_user(
            username="calendar-single-member", password="password", role=User.ROLE_MEMBER
        )
        self.staff = User.objects.create_user(
            username="calendar-single-staff", password="password", is_staff=True
        )
        self.superuser = User.objects.create_superuser(
            username="calendar-single-superuser", password="password", email="admin@example.com"
        )
        self.court = Court.objects.create(name="単発レッスンコート")
        self.other_court = Court.objects.create(name="単発レッスン第2コート")

    def _aware(self, target_date, hour):
        value = datetime.combine(target_date, datetime.min.time()).replace(hour=hour)
        return timezone.make_aware(value)

    def _post_data(self, target_date, **overrides):
        data = {
            "coach": self.coach.pk,
            "court": self.court.pk,
            "lesson_type": CoachAvailability.LESSON_GENERAL,
            "target_level": User.LEVEL_BEGINNER,
            "target_level_2": "",
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
        data.update(overrides)
        return data

    @patch("club.views.timezone.localdate", return_value=today)
    def test_calendar_get_initializes_date_coach_and_general_lesson(self, _localdate):
        self.client.force_login(self.coach)
        target_date = self.today + timedelta(days=3)

        response = self.client.get(
            reverse("club:coach_availability_create"),
            {"date": target_date.isoformat(), "source": "calendar"},
        )

        form = response.context["form"]
        self.assertEqual(form["start_date"].value(), target_date)
        self.assertEqual(form["end_date"].value(), target_date)
        self.assertEqual(form["coach"].value(), self.coach.pk)
        self.assertEqual(form["lesson_type"].value(), CoachAvailability.LESSON_GENERAL)
        self.assertIn("target_level_2", form.fields)

    @patch("club.forms.timezone.localdate", return_value=today)
    def test_general_duration_and_business_hours_are_enforced(self, _localdate):
        self.client.force_login(self.coach)

        short_response = self.client.post(
            reverse("club:coach_availability_create"),
            self._post_data(self.today, end_hour="10"),
        )
        self.assertContains(short_response, "一般レッスンは2時間で登録してください。")

        late_response = self.client.post(
            reverse("club:coach_availability_create"),
            self._post_data(self.today, start_hour="20", end_hour="22"),
        )
        self.assertContains(late_response, "22 は候補にありません。")
        self.assertFalse(CoachAvailability.objects.exists())

        valid_response = self.client.post(
            reverse("club:coach_availability_create"),
            self._post_data(self.today, start_hour="19", end_hour="21"),
        )
        self.assertRedirects(valid_response, reverse("club:coach_availability_list"))
        self.assertTrue(CoachAvailability.objects.filter(start_at=self._aware(self.today, 19)).exists())

    @patch("club.forms.timezone.localdate", return_value=today)
    def test_existing_coach_and_court_overlap_validation_is_used(self, _localdate):
        CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=CoachAvailability.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_at=self._aware(self.today, 9),
            end_at=self._aware(self.today, 11),
        )
        self.client.force_login(self.staff)

        coach_response = self.client.post(
            reverse("club:coach_availability_create"),
            self._post_data(self.today, court=self.other_court.pk),
        )
        self.assertContains(coach_response, "同じコーチで重複する空き時間があります。")

        court_response = self.client.post(
            reverse("club:coach_availability_create"),
            self._post_data(self.today, coach=self.contractor.pk),
        )
        self.assertContains(court_response, "同じコートで重複する空き時間があります。")
        self.assertEqual(CoachAvailability.objects.count(), 1)

    @patch("club.forms.timezone.localdate", return_value=today)
    def test_calendar_source_redirects_to_created_month_without_side_records(self, _localdate):
        self.client.force_login(self.coach)
        target_date = self.today + timedelta(days=6)
        response = self.client.post(
            reverse("club:coach_availability_create"),
            self._post_data(target_date, source="calendar"),
        )

        expected = f"{reverse('club:lesson_calendar')}?year={target_date.year}&month={target_date.month}"
        self.assertRedirects(response, expected)
        self.assertEqual(CoachAvailability.objects.count(), 1)
        self.assertFalse(FixedLesson.objects.exists())
        self.assertFalse(Reservation.objects.exists())

    @patch("club.forms.timezone.localdate", return_value=today)
    def test_non_calendar_source_keeps_existing_redirect(self, _localdate):
        self.client.force_login(self.coach)
        response = self.client.post(
            reverse("club:coach_availability_create"), self._post_data(self.today)
        )
        self.assertRedirects(response, reverse("club:coach_availability_list"))

    @patch("club.views.timezone.localdate", return_value=today)
    def test_created_general_availability_is_rendered_on_calendar(self, _localdate):
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=CoachAvailability.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_at=self._aware(self.today, 9),
            end_at=self._aware(self.today, 11),
        )
        response = self.client.get(
            reverse("club:lesson_calendar"), {"year": self.today.year, "month": self.today.month}
        )
        self.assertContains(response, f'availability_id={availability.pk}')
        self.assertContains(response, "通常レッスン")

    @patch("club.views.timezone.localdate", return_value=today)
    def test_creation_permissions_and_calendar_links(self, _localdate):
        create_url = reverse("club:coach_availability_create")
        calendar_url = reverse("club:lesson_calendar")

        self.client.force_login(self.member)
        member_calendar = self.client.get(calendar_url)
        self.assertNotContains(member_calendar, "＋ 単発レッスン")
        self.assertEqual(self.client.get(create_url).status_code, 403)
        self.assertEqual(self.client.post(create_url, self._post_data(self.today)).status_code, 403)

        for user in (self.coach, self.contractor, self.staff, self.superuser):
            self.client.force_login(user)
            response = self.client.get(calendar_url)
            self.assertContains(response, "＋ 単発レッスン")
            self.assertContains(response, "source=calendar")
            self.assertContains(response, 'event.target.closest("a, button, input, select, textarea, form")')
            self.assertEqual(self.client.get(create_url).status_code, 200)

    def test_contractor_is_initialized_as_assigned_coach(self):
        self.client.force_login(self.contractor)
        response = self.client.get(reverse("club:coach_availability_create"))
        form = response.context["form"]
        self.assertEqual(form["coach"].value(), self.contractor.pk)
        self.assertEqual(list(form.fields["coach"].queryset), [self.contractor])

    def test_model_rejects_overlapping_slots(self):
        CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            start_at=self._aware(self.today, 9),
            end_at=self._aware(self.today, 11),
        )
        with self.assertRaises(ValidationError):
            CoachAvailability.objects.create(
                coach=self.coach,
                court=self.other_court,
                start_at=self._aware(self.today, 10),
                end_at=self._aware(self.today, 12),
            )
