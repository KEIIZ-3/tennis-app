from datetime import date, datetime, timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.forms import CoachAvailabilityForm
from club.models import CoachAvailability, Court, FixedLesson, Reservation, User
from club.admin import CoachAvailabilityAdmin
from django.contrib import admin
from club import lesson_execution
from club.settlement_calculator import reservation_coaches_for_split


class CalendarSingleLessonCreationTests(TestCase):
    today = timezone.localdate()

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
        self.court = Court.objects.create(
            name="カレンダー単発コート", available_court_count=12
        )
        self.other_court = Court.objects.create(name="カレンダー単発第2コート")

    def _aware(self, target_date, hour):
        value = datetime.combine(target_date, datetime.min.time()).replace(hour=hour)
        return timezone.make_aware(value)

    def _post_data(self, *, coach=None, court=None, start_hour=9, end_hour=11):
        return {
            "source": "calendar",
            "coach": (coach or self.coach).pk,
            "coach_2": "",
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

    def test_court_capacity_error_is_rendered_as_form_error_without_saving(self):
        existing = CoachAvailability.objects.create(
            coach=self.other_coach,
            court=self.court,
            start_at=self._aware(self.target_date, 9),
            end_at=self._aware(self.target_date, 11),
        )
        CoachAvailability.objects.filter(pk=existing.pk).update(court_count=12)
        self.client.force_login(self.coach)

        response = self.client.post(
            reverse("club:coach_availability_create"), self._post_data()
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(CoachAvailability.objects.count(), 1)
        self.assertContains(response, "利用可能コート面数を超えています")
        self.assertContains(response, 'name="source" value="calendar"')
        self.assertEqual(
            response.context["form"]["start_date"].value(),
            self.target_date.isoformat(),
        )

    def test_coach_overlap_is_rendered_as_form_error_without_saving(self):
        CoachAvailability.objects.create(
            coach=self.coach,
            court=self.other_court,
            start_at=self._aware(self.target_date, 9),
            end_at=self._aware(self.target_date, 11),
        )
        self.client.force_login(self.coach)

        response = self.client.post(
            reverse("club:coach_availability_create"), self._post_data()
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(CoachAvailability.objects.count(), 1)
        self.assertContains(response, "同じコーチで重複する空き時間があります。")

    def test_edit_overlap_is_rendered_as_form_error_without_updating(self):
        existing = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.other_court,
            start_at=self._aware(self.target_date, 9),
            end_at=self._aware(self.target_date, 11),
        )
        self.other_court.available_court_count = 1
        self.other_court.save(update_fields=["available_court_count"])
        edited = CoachAvailability.objects.create(
            coach=self.other_coach,
            court=self.court,
            start_at=self._aware(self.target_date, 11),
            end_at=self._aware(self.target_date, 13),
        )
        self.client.force_login(self.staff)
        data = self._post_data(coach=self.other_coach, court=self.other_court)

        response = self.client.post(
            reverse("club:coach_availability_edit", args=[edited.pk]), data
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(CoachAvailability.objects.count(), 2)
        self.assertContains(response, "利用可能コート面数を超えています")
        edited.refresh_from_db()
        self.assertEqual(edited.court, self.court)
        self.assertEqual(existing.court, self.other_court)

    def test_start_hour_script_assists_only_valid_two_hour_end_times(self):
        self.client.force_login(self.coach)
        response = self.client.get(
            reverse("club:coach_availability_create"),
            {"date": self.target_date.isoformat(), "source": "calendar"},
        )
        self.assertContains(response, "const assistedEndHour = hour + 2;")
        self.assertContains(response, "assistedEndHour > 21")

    def test_model_rejects_coach_overlap_and_allows_court_sharing_within_capacity(self):
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
        same_court = CoachAvailability.objects.create(
            coach=self.other_coach,
            court=self.court,
            start_at=self._aware(self.target_date, 10),
            end_at=self._aware(self.target_date, 12),
        )
        self.assertIsNotNone(same_court.pk)
        allowed = CoachAvailability.objects.create(
            coach=self.contractor,
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

    def test_two_coaches_are_canonical_and_totals_use_capacity_policy(self):
        data = self._post_data()
        data.update({"coach_2": self.other_coach.pk, "coach_count": 99, "court_count": 99, "capacity": 99})
        form = CoachAvailabilityForm(data=data, request_user=self.staff)
        self.assertTrue(form.is_valid(), form.errors)
        availability = form.save()
        self.assertEqual((availability.coach_count, availability.court_count, availability.capacity), (2, 2, 10))
        self.assertEqual(availability.coach_display_names(), f"{self.coach.display_name()} / {self.other_coach.display_name()}")

    def test_one_coach_totals_are_normalized(self):
        form = CoachAvailabilityForm(data=self._post_data(), request_user=self.staff)
        self.assertTrue(form.is_valid(), form.errors)
        availability = form.save()
        self.assertEqual((availability.coach_count, availability.court_count, availability.capacity), (1, 1, 5))

    def test_duplicate_second_coach_is_rejected(self):
        with self.assertRaises(ValidationError):
            CoachAvailability.objects.create(
                coach=self.coach, coach_2=self.coach, court=self.court,
                start_at=self._aware(self.target_date, 9), end_at=self._aware(self.target_date, 11),
            )

    def test_overlap_checks_both_formal_coach_positions(self):
        CoachAvailability.objects.create(
            coach=self.coach, coach_2=self.other_coach, court=self.court,
            start_at=self._aware(self.target_date, 9), end_at=self._aware(self.target_date, 11),
        )
        with self.assertRaises(ValidationError):
            CoachAvailability.objects.create(
                coach=self.contractor, coach_2=self.coach, court=self.other_court,
                start_at=self._aware(self.target_date, 10), end_at=self._aware(self.target_date, 12),
            )

    def test_calendar_and_member_list_show_both_coaches(self):
        availability = CoachAvailability.objects.create(
            coach=self.coach, coach_2=self.other_coach, court=self.court,
            start_at=self._aware(self.target_date, 9), end_at=self._aware(self.target_date, 11),
        )
        self.client.force_login(self.coach)
        calendar = self.client.get(reverse("club:lesson_calendar"), {"year": self.target_date.year, "month": self.target_date.month})
        members = self.client.get(reverse("club:lesson_calendar_member_list"), {"availability_id": availability.pk})
        self.assertContains(calendar, self.coach.display_name())
        self.assertContains(calendar, self.other_coach.display_name())
        self.assertContains(members, availability.coach_display_names())

    def test_2026_09_26_is_in_admin_queryset_and_visible(self):
        target = date(2026, 9, 26)
        availability = CoachAvailability.objects.create(
            coach=self.coach, coach_2=self.other_coach, court=self.court,
            start_at=self._aware(target, 9), end_at=self._aware(target, 11),
        )
        model_admin = CoachAvailabilityAdmin(CoachAvailability, admin.site)
        request = self.client.request().wsgi_request
        self.assertIn(availability, model_admin.get_queryset(request))
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("admin:club_coachavailability_changelist"))
        self.assertContains(response, "2026年9月26日")
        self.assertContains(response, self.other_coach.username)

    def test_one_time_lesson_is_visible_from_the_regular_lesson_admin(self):
        availability = CoachAvailability.objects.create(
            coach=self.coach, coach_2=self.other_coach, court=self.court,
            start_at=self._aware(self.target_date, 9), end_at=self._aware(self.target_date, 11),
        )
        Reservation.objects.create(
            user=self.member, coach=self.coach, court=self.court,
            availability=availability, start_at=availability.start_at,
            end_at=availability.end_at,
        )

        self.client.force_login(self.superuser)
        response = self.client.get(reverse("admin:club_fixedlesson_changelist"))

        self.assertContains(response, "1回開催レッスン")
        self.assertContains(
            response,
            reverse("admin:club_coachavailability_change", args=[availability.pk]),
        )
        self.assertContains(response, availability.coach_display_names())
        self.assertContains(response, "1 / 10名")

    def test_general_one_time_lesson_uses_common_execution_slots(self):
        availability = CoachAvailability.objects.create(
            coach=self.coach, coach_2=self.other_coach, court=self.court,
            start_at=self._aware(self.target_date, 9), end_at=self._aware(self.target_date, 11),
        )

        slots = lesson_execution._canonical_slots(
            self.target_date.year, self.target_date.month
        )

        slot = next(row for row in slots if row["availability"].pk == availability.pk)
        self.assertIsNone(slot["fixed_lesson"])
        self.assertEqual(slot["source_kind"], "availability")
        self.assertEqual(
            slot["coach_names"],
            [self.coach.display_name(), self.other_coach.display_name()],
        )

    def test_two_coach_availability_is_used_by_existing_revenue_split(self):
        availability = CoachAvailability.objects.create(
            coach=self.coach, coach_2=self.other_coach, court=self.court,
            start_at=self._aware(self.target_date, 9), end_at=self._aware(self.target_date, 11),
        )
        reservation = Reservation.objects.create(
            user=self.member, coach=self.coach, court=self.court, availability=availability,
            start_at=availability.start_at, end_at=availability.end_at,
        )
        self.assertEqual(reservation_coaches_for_split(reservation), [self.coach, self.other_coach])

    def test_delete_permissions_reservations_and_redirect(self):
        availability = CoachAvailability.objects.create(
            coach=self.coach, coach_2=self.other_coach, court=self.court,
            start_at=self._aware(self.target_date, 9), end_at=self._aware(self.target_date, 11),
        )
        url = reverse("club:coach_availability_delete", args=[availability.pk])
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.force_login(self.other_coach)
        response = self.client.post(url)
        self.assertRedirects(response, f"{reverse('club:lesson_calendar')}?year={self.target_date.year}&month={self.target_date.month}")
        self.assertFalse(CoachAvailability.objects.filter(pk=availability.pk).exists())

    def test_delete_is_blocked_when_any_reservation_history_exists(self):
        availability = CoachAvailability.objects.create(
            coach=self.coach, court=self.court,
            start_at=self._aware(self.target_date, 9), end_at=self._aware(self.target_date, 11),
        )
        Reservation.objects.create(
            user=self.member, coach=self.coach, court=self.court, availability=availability,
            start_at=availability.start_at, end_at=availability.end_at,
            status=Reservation.STATUS_CANCELED,
        )
        self.client.force_login(self.coach)
        response = self.client.post(reverse("club:coach_availability_delete", args=[availability.pk]))
        self.assertRedirects(response, f"{reverse('club:lesson_calendar_member_list')}?availability_id={availability.pk}")
        self.assertTrue(CoachAvailability.objects.filter(pk=availability.pk).exists())
