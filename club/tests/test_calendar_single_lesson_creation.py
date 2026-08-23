from datetime import date, datetime, timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.forms import CoachAvailabilityForm
from club.models import CoachAvailability, Court, FixedLesson, Reservation, User


class CalendarSingleLessonCreationTests(TestCase):
    today = date(2026, 8, 23)

    def setUp(self):
        self.target_date = self.today + timedelta(days=5)
        self.coach = User.objects.create_user(
            username="calendar-coach", password="password", role=User.ROLE_COACH
        )
        self.contractor = User.objects.create_user(
            username="calendar-contractor", password="password", role=User.ROLE_CONTRACTOR_COACH
        )
        self.staff = User.objects.create_user(
            username="calendar-staff", password="password", role="staff"
        )
        self.superuser = User.objects.create_superuser(
            username="calendar-superuser", password="password"
        )
        self.member = User.objects.create_user(
            username="calendar-member", password="password", role="member"
        )
        self.other_coach = User.objects.create_user(
            username="calendar-other-coach", password="password", role=User.ROLE_COACH
        )
        self.court = Court.objects.create(name="カレンダー単発コート")
        self.other_court = Court.objects.create(name="カレンダー単発第2コート")

    def _aware(self, target_date, hour):
        value = datetime.combine(target_date, datetime.min.time()).replace(hour=hour)
        return timezone.make_aware(value)

    def _post_data(self, *, coach=None, court=None, start_hour=9, end_hour=11):
        return {
            "source": "calendar",
            "coach": (coach or self.coach).pk,
            "substitute_coach": "",
            "court": (court or self.court).pk,
            "lesson_type": CoachAvailability.LESSON_GENERAL,
            "target_level": User.LEVEL_BEGINNER,
            "target_level_2": "",
            "coach_count": 1,
            "court_count": 1,
            "capacity": 6,
            "custom_ticket_price": 0,
            "custom_duration_hours": 0,
            "note": "",
            "start_date": self.target_date.isoformat(),
            "start_hour": str(start_hour),
            "end_date": self.target_date.isoformat(),
            "end_hour": str(end_hour),
        }

    @patch("club.views.timezone.localdate", return_value=today)
    def test_calendar_link_preserves_source_and_initializes_general_lesson(self, _localdate):
        self.client.force_login(self.coach)
        calendar_response = self.client.get(
            reverse("club:lesson_calendar"),
            {"year": self.target_date.year, "month": self.target_date.month},
        )
        expected_query = f"date={self.target_date.isoformat()}&amp;source=calendar"
        self.assertContains(calendar_response, expected_query)

        response = self.client.get(
            reverse("club:coach_availability_create"),
            {"date": self.target_date.isoformat(), "source": "calendar"},
        )
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(form["start_date"].value(), self.target_date)
        self.assertEqual(form["end_date"].value(), self.target_date)
        self.assertEqual(form["lesson_type"].value(), CoachAvailability.LESSON_GENERAL)
        self.assertEqual(form["coach"].value(), self.coach.pk)
        self.assertContains(response, 'name="source" value="calendar"')

    def test_form_includes_both_coach_roles_and_second_target_level(self):
        form = CoachAvailabilityForm(request_user=self.staff)
        self.assertQuerySetEqual(
            form.fields["coach"].queryset,
            User.objects.filter(role__in=User.COACH_ROLE_VALUES).order_by("username", "id"),
        )
        self.assertEqual(form.fields["target_level_2"].label, "第2対象レベル")
        self.assertFalse(form.fields["target_level_2"].required)

    @patch("club.views.timezone.localdate", return_value=today)
    def test_authorized_roles_can_open_calendar_creation(self, _localdate):
        for user in (self.coach, self.contractor, self.staff, self.superuser):
            with self.subTest(role=user.role, superuser=user.is_superuser):
                self.client.force_login(user)
                response = self.client.get(
                    reverse("club:coach_availability_create"),
                    {"date": self.target_date.isoformat(), "source": "calendar"},
                )
                self.assertEqual(response.status_code, 200)

    @patch("club.views.timezone.localdate", return_value=today)
    def test_contractor_is_initialized_as_the_assigned_coach(self, _localdate):
        self.client.force_login(self.contractor)
        response = self.client.get(
            reverse("club:coach_availability_create"),
            {"date": self.target_date.isoformat(), "source": "calendar"},
        )
        self.assertEqual(response.context["form"]["coach"].value(), self.contractor.pk)
        self.assertQuerySetEqual(
            response.context["form"].fields["coach"].queryset,
            User.objects.filter(pk=self.contractor.pk),
        )

    @patch("club.views.timezone.localdate", return_value=today)
    def test_member_has_no_calendar_link_and_direct_get_or_post_is_forbidden(self, _localdate):
        self.client.force_login(self.member)
        calendar_response = self.client.get(
            reverse("club:lesson_calendar"),
            {"year": self.target_date.year, "month": self.target_date.month},
        )
        self.assertNotContains(calendar_response, "＋ 単発レッスン")
        create_url = reverse("club:coach_availability_create")
        self.assertEqual(
            self.client.get(create_url, {"date": self.target_date.isoformat(), "source": "calendar"}).status_code,
            403,
        )
        self.assertEqual(self.client.post(create_url, self._post_data()).status_code, 403)
        self.assertFalse(CoachAvailability.objects.exists())

    def test_calendar_general_lesson_duration_and_business_hours_are_server_validated(self):
        self.client.force_login(self.coach)
        invalid_duration = self.client.post(
            reverse("club:coach_availability_create"), self._post_data(end_hour=10)
        )
        self.assertContains(invalid_duration, "一般レッスンは2時間で登録してください。")
        outside_business_hours = self.client.post(
            reverse("club:coach_availability_create"), self._post_data(start_hour=20, end_hour=21)
        )
        self.assertContains(outside_business_hours, "一般レッスンは2時間で登録してください。")
        self.assertFalse(CoachAvailability.objects.exists())

    def test_1900_to_2100_is_created_and_redirects_to_selected_month(self):
        self.client.force_login(self.coach)
        response = self.client.post(
            reverse("club:coach_availability_create"), self._post_data(start_hour=19, end_hour=21)
        )
        self.assertRedirects(
            response,
            f"{reverse('club:lesson_calendar')}?year={self.target_date.year}&month={self.target_date.month}",
        )
        availability = CoachAvailability.objects.get()
        self.assertEqual(availability.coach, self.coach)
        self.assertEqual(FixedLesson.objects.count(), 0)
        self.assertEqual(Reservation.objects.count(), 0)

    def test_non_calendar_creation_keeps_existing_redirect(self):
        self.client.force_login(self.coach)
        data = self._post_data()
        data.pop("source")
        response = self.client.post(reverse("club:coach_availability_create"), data)
        self.assertRedirects(response, reverse("club:coach_availability_list"))

    def test_start_hour_script_assists_only_valid_two_hour_end_times(self):
        self.client.force_login(self.coach)
        response = self.client.get(
            reverse("club:coach_availability_create"),
            {"date": self.target_date.isoformat(), "source": "calendar"},
        )
        self.assertContains(response, "const assistedEndHour = hour + 2;")
        self.assertContains(response, "assistedEndHour > 21")

    def test_model_rejects_coach_and_court_overlaps_but_allows_distinct_resources(self):
        CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=CoachAvailability.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_at=self._aware(self.target_date, 9),
            end_at=self._aware(self.target_date, 11),
        )
        with self.assertRaisesMessage(ValidationError, "同じコーチで重複する空き時間があります。"):
            CoachAvailability.objects.create(
                coach=self.coach,
                court=self.other_court,
                start_at=self._aware(self.target_date, 10),
                end_at=self._aware(self.target_date, 12),
            )
        with self.assertRaisesMessage(ValidationError, "同じコートで重複する空き時間があります。"):
            CoachAvailability.objects.create(
                coach=self.other_coach,
                court=self.court,
                start_at=self._aware(self.target_date, 10),
                end_at=self._aware(self.target_date, 12),
            )
        allowed = CoachAvailability.objects.create(
            coach=self.other_coach,
            court=self.other_court,
            start_at=self._aware(self.target_date, 9),
            end_at=self._aware(self.target_date, 11),
        )
        self.assertIsNotNone(allowed.pk)

    def test_created_availability_appears_as_normal_bookable_calendar_lesson(self):
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=CoachAvailability.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_at=self._aware(self.target_date, 9),
            end_at=self._aware(self.target_date, 11),
        )
        self.member.member_level = User.LEVEL_BEGINNER
        self.member.email = "calendar-member@example.com"
        self.member.phone_number = "09012345678"
        self.member.is_profile_completed = True
        self.member.save(update_fields=["member_level", "email", "phone_number", "is_profile_completed"])
        self.client.force_login(self.member)
        response = self.client.get(
            reverse("club:lesson_calendar"),
            {"year": self.target_date.year, "month": self.target_date.month},
        )
        self.assertContains(response, f"availability_id={availability.pk}")
        self.assertContains(response, reverse("club:reservation_create"))
        self.assertEqual(FixedLesson.objects.count(), 0)
        self.assertEqual(Reservation.objects.count(), 0)

