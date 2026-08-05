from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.models import CoachAvailability, Court, FixedLesson, Reservation


class CustomerReservationUiTests(TestCase):
    def setUp(self):
        self.User = get_user_model()
        self.member = self._user("customer", self.User.ROLE_MEMBER)
        self.other_member = self._user("other", self.User.ROLE_MEMBER)
        self.coach = self._user("coach", self.User.ROLE_COACH)
        self.staff = self._user("staff", self.User.ROLE_MEMBER, is_staff=True)
        self.court = Court.objects.create(
            name="とても長い名前のテストコート",
            is_active=True,
            court_type=Court.COURT_SONO,
        )
        lesson_date = timezone.localdate() + timedelta(days=10)
        self.start_at = timezone.make_aware(datetime.combine(lesson_date, time(19, 0)))
        self.end_at = self.start_at + timedelta(hours=2)
        self.fixed_lesson = FixedLesson.objects.create(
            title="スマートフォン表示を確認する長いレッスン名",
            coach=self.coach,
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            start_date=timezone.localdate(self.start_at),
            weekday=timezone.localdate(self.start_at).weekday(),
            start_hour=timezone.localtime(self.start_at).hour,
            capacity=6,
            is_active=True,
        )

    def _user(self, username, role, **extra):
        user = self.User.objects.create_user(
            username=username,
            password="password12345",
            full_name=f"{username} 表示名",
            role=role,
            member_level=self.User.LEVEL_BEGINNER,
            is_profile_completed=True,
            **extra,
        )
        return user

    def _reservation(self, *, user=None, status=Reservation.STATUS_ACTIVE, fixed=True, day_offset=0):
        start_at = self.start_at + timedelta(days=day_offset)
        availability, _created = CoachAvailability.objects.get_or_create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            start_at=start_at,
            end_at=start_at + timedelta(hours=2),
            defaults={
                "target_level": self.User.LEVEL_BEGINNER,
                "capacity": 6,
                "status": CoachAvailability.STATUS_OPEN,
            },
        )
        return Reservation.objects.create(
            user=user or self.member,
            coach=self.coach,
            court=self.court,
            availability=availability,
            fixed_lesson=self.fixed_lesson if fixed else None,
            is_fixed_entry=fixed,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=start_at + timedelta(hours=2),
            status=status,
        )

    def test_fixed_and_single_reservations_share_active_confirmation_list(self):
        fixed = self._reservation(fixed=True)
        single = self._reservation(fixed=False, day_offset=1)
        canceled = self._reservation(status=Reservation.STATUS_CANCELED, fixed=False, day_offset=2)
        self.client.force_login(self.member)

        response = self.client.get(reverse("club:reservation_list"))

        self.assertEqual(
            [row["reservation"].pk for row in response.context["future_reservation_rows"]],
            [fixed.pk, single.pk],
        )
        self.assertEqual(
            [row["reservation"].pk for row in response.context["canceled_reservation_rows"]],
            [canceled.pk],
        )
        self.assertContains(response, "キャンセル済み")
        self.assertContains(response, "現在の参加人数には含まれません")

    def test_customer_templates_expose_mobile_structure_and_text_statuses(self):
        self._reservation()
        self.client.force_login(self.member)

        list_response = self.client.get(reverse("club:reservation_list"))
        calendar_response = self.client.get(
            reverse("club:lesson_calendar"),
            {"year": self.start_at.year, "month": self.start_at.month},
        )

        self.assertContains(list_response, "@media(max-width:768px)", html=False)
        self.assertContains(list_response, "status-badge")
        self.assertContains(calendar_response, "lesson-status-label")
        self.assertContains(calendar_response, "空きあり")
        self.assertContains(calendar_response, "overflow-x:hidden")

    def test_customer_cancel_entry_matches_backend_permissions(self):
        own = self._reservation()
        self._reservation(user=self.other_member)
        other = self._reservation(user=self.other_member, fixed=False, day_offset=1)

        self.client.force_login(self.member)
        own_detail = self.client.get(reverse("club:reservation_detail", args=[own.pk]))
        other_cancel = self.client.post(reverse("club:reservation_cancel", args=[other.pk]))
        self.assertContains(own_detail, "この予約をキャンセル")
        self.assertEqual(other_cancel.status_code, 403)

        self.client.force_login(self.coach)
        coach_detail = self.client.get(reverse("club:reservation_detail", args=[other.pk]))
        coach_cancel = self.client.post(reverse("club:reservation_cancel", args=[other.pk]))
        self.assertNotContains(coach_detail, "この予約をキャンセル")
        self.assertEqual(coach_cancel.status_code, 403)

        self.client.force_login(self.staff)
        staff_cancel = self.client.post(reverse("club:reservation_cancel", args=[other.pk]))
        self.assertEqual(staff_cancel.status_code, 302)

    def test_cancelled_and_rain_cancelled_detail_have_explicit_labels(self):
        canceled = self._reservation(status=Reservation.STATUS_CANCELED)
        rain = self._reservation(status=Reservation.STATUS_RAIN_CANCELED, fixed=False, day_offset=1)
        self.client.force_login(self.member)

        self.assertContains(
            self.client.get(reverse("club:reservation_detail", args=[canceled.pk])),
            "キャンセル済みです",
        )
        self.assertContains(
            self.client.get(reverse("club:reservation_detail", args=[rain.pk])),
            "雨天中止として処理されています",
        )
