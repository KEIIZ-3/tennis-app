from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.fixed_lesson_sync_facade import synchronize_fixed_lesson_membership
from club.models import Court, FixedLesson, Reservation, User


class FixedLessonCancelCoachViewsTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.coach = User.objects.create_user(
            username="cancel-coach-view-coach",
            password="test-password",
            role=User.ROLE_COACH,
            full_name="キャンセル表示確認コーチ",
            member_level=User.LEVEL_ADVANCED,
        )
        self.member = User.objects.create_user(
            username="cancel-coach-view-member",
            password="test-password",
            role=User.ROLE_MEMBER,
            full_name="キャンセル表示確認会員",
            member_level=User.LEVEL_BEGINNER,
            ticket_balance=4,
            is_profile_completed=True,
            phone_number="08000000001",
            email="cancel-coach-view@example.com",
        )
        self.court = Court.objects.create(
            name="キャンセル表示確認コート",
            court_type=Court.COURT_OTHER,
        )
        target_weekday = (self.today.weekday() + 1) % 7
        self.fixed_lesson = FixedLesson.objects.create(
            title="キャンセル表示確認固定レッスン",
            coach=self.coach,
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_date=self.today,
            weekday=target_weekday,
            start_hour=19,
            capacity=5,
            coach_count=1,
            court_count=1,
            weeks_ahead=2,
            is_active=True,
        )
        self.fixed_lesson.members.add(self.member)
        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)
        self.reservations = list(
            Reservation.objects.filter(
                user=self.member,
                fixed_lesson=self.fixed_lesson,
                is_fixed_entry=True,
                status=Reservation.STATUS_ACTIVE,
            ).order_by("start_at")
        )
        self.assertEqual(len(self.reservations), 2)
        self.cancelled = self.reservations[0]
        self.future = self.reservations[1]
        self.cancelled.cancel(
            created_by=self.member,
            reason="会員が予約確認画面からキャンセル",
        )
        self.client.force_login(self.coach)

    def test_member_list_does_not_restore_cancelled_fixed_member(self):
        response = self.client.get(
            reverse("club:lesson_calendar_member_list"),
            {
                "availability_id": self.cancelled.availability_id,
                "fixed_lesson_id": self.fixed_lesson.pk,
                "lesson_date": timezone.localtime(
                    self.cancelled.start_at
                ).date().isoformat(),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_count"], 0)
        self.assertEqual(response.context["remaining_count"], 5)
        self.assertEqual(response.context["active_rows"], [])
        self.assertNotContains(response, self.member.display_name())

    def test_weekly_list_counts_only_active_occurrence_reservations(self):
        response = self.client.get(reverse("club:coach_fixed_lesson_weekly"))
        self.assertEqual(response.status_code, 200)

        cancelled_date = timezone.localtime(self.cancelled.start_at).date()
        future_date = timezone.localtime(self.future.start_at).date()
        rows = response.context["fixed_lessons"]
        cancelled_row = next(
            row
            for row in rows
            if row["fixed_lesson"].pk == self.fixed_lesson.pk
            and row["target_date"] == cancelled_date
        )
        future_row = next(
            row
            for row in rows
            if row["fixed_lesson"].pk == self.fixed_lesson.pk
            and row["target_date"] == future_date
        )

        self.assertEqual(cancelled_row["member_count"], 0)
        self.assertEqual(cancelled_row["reservation_count"], 0)
        self.assertNotIn(self.member.display_name(), cancelled_row["member_names"])

        self.assertEqual(future_row["member_count"], 1)
        self.assertEqual(future_row["reservation_count"], 1)
        self.assertIn(self.member.display_name(), future_row["member_names"])

        self.fixed_lesson.refresh_from_db()
        self.assertTrue(
            self.fixed_lesson.members.filter(pk=self.member.pk).exists()
        )
