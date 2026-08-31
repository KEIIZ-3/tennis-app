from datetime import datetime, time, timedelta

from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from club.admin import CoachAvailabilityAdmin
from club.fixed_lesson_membership_service import synchronize_fixed_lesson_membership
from club.models import (
    CoachAvailability,
    Court,
    FixedLesson,
    FixedLessonCanceledOccurrence,
    LessonWaitlist,
    Reservation,
    User,
)


class FixedLessonOccurrenceAdminDeleteTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(
            username="occurrence-admin-coach", role=User.ROLE_COACH
        )
        self.member = User.objects.create_user(
            username="occurrence-admin-member",
            role=User.ROLE_MEMBER,
            member_level=User.LEVEL_BEGINNER,
            ticket_balance=4,
        )
        self.waiting_member = User.objects.create_user(
            username="occurrence-admin-waiting",
            role=User.ROLE_MEMBER,
            member_level=User.LEVEL_BEGINNER,
        )
        self.admin_user = User.objects.create_superuser(
            username="occurrence-admin", email="admin@example.com", password="pw"
        )
        self.court = Court.objects.create(name="開催回中止テストコート")
        first_date = timezone.localdate() + timedelta(days=1)
        self.fixed_lesson = FixedLesson.objects.create(
            title="開催回中止テスト",
            coach=self.coach,
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_date=first_date,
            weekday=first_date.weekday(),
            start_hour=19,
            weeks_ahead=3,
            is_active=True,
        )
        self.fixed_lesson.members.add(self.member)
        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)
        self.occurrence_dates = self.fixed_lesson.scheduled_occurrence_dates()
        self.target_date = self.occurrence_dates[1]
        self.target_reservation = Reservation.objects.get(
            fixed_lesson=self.fixed_lesson,
            user=self.member,
            start_at__date=self.target_date,
            status=Reservation.STATUS_ACTIVE,
        )
        self.target_availability = self.target_reservation.availability
        self.waitlist = LessonWaitlist.objects.create(
            user=self.waiting_member,
            coach=self.coach,
            court=self.court,
            availability=self.target_availability,
            fixed_lesson=self.fixed_lesson,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_at=self.target_reservation.start_at,
            end_at=self.target_reservation.end_at,
        )
        request = RequestFactory().post("/admin/club/coachavailability/")
        request.user = self.admin_user
        CoachAvailabilityAdmin(CoachAvailability, AdminSite()).delete_model(
            request, self.target_availability
        )

    def test_admin_delete_cancels_only_selected_fixed_occurrence(self):
        self.fixed_lesson.refresh_from_db()
        self.target_reservation.refresh_from_db()
        self.waitlist.refresh_from_db()

        self.assertTrue(FixedLesson.objects.filter(pk=self.fixed_lesson.pk).exists())
        self.assertTrue(
            FixedLessonCanceledOccurrence.objects.filter(
                fixed_lesson=self.fixed_lesson,
                occurrence_date=self.target_date,
            ).exists()
        )
        self.assertNotIn(self.target_date, self.fixed_lesson.scheduled_occurrence_dates())
        self.assertEqual(self.target_reservation.status, Reservation.STATUS_CANCELED)
        self.assertEqual(self.waitlist.status, LessonWaitlist.STATUS_CANCELED)
        self.assertFalse(CoachAvailability.objects.filter(pk=self.target_availability.pk).exists())
        other_active_dates = set(
            Reservation.objects.filter(
                fixed_lesson=self.fixed_lesson,
                status=Reservation.STATUS_ACTIVE,
            ).values_list("start_at__date", flat=True)
        )
        self.assertSetEqual(other_active_dates, {self.occurrence_dates[0], self.occurrence_dates[2]})

    def test_sync_does_not_recreate_canceled_occurrence_or_refund_twice(self):
        ticket_balance = self.member.ticket_balance
        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)
        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)

        self.assertFalse(
            CoachAvailability.objects.filter(
                coach=self.coach,
                start_at__date=self.target_date,
            ).exists()
        )
        self.assertFalse(
            Reservation.objects.filter(
                fixed_lesson=self.fixed_lesson,
                start_at__date=self.target_date,
                status__in=(Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING),
            ).exists()
        )
        self.member.refresh_from_db()
        self.assertEqual(self.member.ticket_balance, ticket_balance)

    def test_calendar_omits_only_canceled_occurrence(self):
        self.client.force_login(self.member)
        response = self.client.get(
            reverse("club:lesson_calendar"),
            {"year": self.target_date.year, "month": self.target_date.month},
        )
        self.assertEqual(response.status_code, 200)
        displayed_dates = {
            row["lesson_date"]
            for row in response.context["schedule_rows"]
            if row.get("fixed_lesson_id") == str(self.fixed_lesson.pk)
        }
        self.assertNotIn(self.target_date.isoformat(), displayed_dates)
        for occurrence_date in (self.occurrence_dates[0], self.occurrence_dates[2]):
            if occurrence_date.month == self.target_date.month:
                self.assertIn(occurrence_date.isoformat(), displayed_dates)


class RegularAvailabilityAdminDeleteTests(TestCase):
    def test_regular_availability_is_deleted_without_cancellation_record(self):
        coach = User.objects.create_user(username="regular-admin-coach", role=User.ROLE_COACH)
        admin_user = User.objects.create_superuser(
            username="regular-admin", email="regular@example.com", password="pw"
        )
        court = Court.objects.create(name="通常枠削除テストコート")
        target_date = timezone.localdate() + timedelta(days=2)
        start_at = timezone.make_aware(datetime.combine(target_date, time(10, 0)))
        availability = CoachAvailability.objects.create(
            coach=coach,
            court=court,
            lesson_type=CoachAvailability.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=start_at + timedelta(hours=2),
        )
        request = RequestFactory().post("/admin/club/coachavailability/")
        request.user = admin_user

        CoachAvailabilityAdmin(CoachAvailability, AdminSite()).delete_model(request, availability)

        self.assertFalse(CoachAvailability.objects.filter(pk=availability.pk).exists())
        self.assertFalse(FixedLessonCanceledOccurrence.objects.exists())
