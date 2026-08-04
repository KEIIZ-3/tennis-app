from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.fixed_lesson_sync_facade import synchronize_fixed_lesson_membership
from club.court_number_line_notice import _slot_participants
from club.lesson_participants import reservations_for_object
from club.models import FixedLesson, Reservation, User


class FixedLessonOccurrenceCancelCalendarTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.coach = User.objects.create_user(
            username="occurrence-cancel-coach",
            password="test-password",
            role=User.ROLE_COACH,
            full_name="開催回キャンセル担当",
            member_level=User.LEVEL_ADVANCED,
        )
        self.member = User.objects.create_user(
            username="occurrence-cancel-member",
            password="test-password",
            role=User.ROLE_MEMBER,
            full_name="開催回キャンセル会員",
            member_level=User.LEVEL_BEGINNER,
            ticket_balance=4,
            is_profile_completed=True,
            phone_number="08000000000",
            email="occurrence@example.com",
        )
        target_weekday = (self.today.weekday() + 1) % 7
        self.fixed_lesson = FixedLesson.objects.create(
            title="開催回キャンセル検証",
            coach=self.coach,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_date=self.today,
            weekday=target_weekday,
            start_hour=19,
            capacity=1,
            coach_count=1,
            court_count=1,
            weeks_ahead=2,
            is_active=True,
        )
        self.fixed_lesson.members.add(self.member)
        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)
        self.client.force_login(self.member)

    def test_cancelled_occurrence_is_not_counted_or_forced_reserved(self):
        reservations = list(
            Reservation.objects.filter(
                user=self.member,
                fixed_lesson=self.fixed_lesson,
                is_fixed_entry=True,
                status=Reservation.STATUS_ACTIVE,
            ).order_by("start_at")
        )
        self.assertEqual(len(reservations), 2)

        cancelled = reservations[0]
        future = reservations[1]
        cancelled.cancel(
            created_by=self.member,
            reason="会員が予約確認画面からキャンセル",
        )

        response = self.client.get(
            reverse("club:lesson_calendar"),
            {
                "year": timezone.localtime(cancelled.start_at).year,
                "month": timezone.localtime(cancelled.start_at).month,
            },
        )
        self.assertEqual(response.status_code, 200)

        cancelled_date = timezone.localtime(cancelled.start_at).date().isoformat()
        future_date = timezone.localtime(future.start_at).date().isoformat()
        rows = response.context["schedule_rows"]
        cancelled_row = next(row for row in rows if row.get("lesson_date") == cancelled_date)
        future_row = next(row for row in rows if row.get("lesson_date") == future_date)

        self.assertEqual(cancelled_row["member_count"], 0)
        self.assertFalse(cancelled_row["is_reserved_by_user"] )
        self.assertTrue(cancelled_row["can_book"] )
        self.assertFalse(cancelled_row["can_join_waitlist"] )

        self.assertEqual(future_row["member_count"], 1)
        self.assertTrue(future_row["is_reserved_by_user"] )

        self.fixed_lesson.refresh_from_db()
        self.assertTrue(self.fixed_lesson.members.filter(pk=self.member.pk).exists())

    def test_cancelled_fixed_member_is_excluded_from_all_participant_consumers(self):
        reservation = Reservation.objects.filter(
            user=self.member,
            fixed_lesson=self.fixed_lesson,
            status=Reservation.STATUS_ACTIVE,
        ).order_by("start_at").first()
        reservation.cancel(
            created_by=self.member,
            reason="会員が予約確認画面からキャンセル",
        )

        self.assertEqual(reservations_for_object(reservation).count(), 0)
        self.assertEqual(_slot_participants(reservation).count(), 0)

        target_date = timezone.localtime(reservation.start_at).date()
        response = self.client.get(
            reverse("club:lesson_reservation_confirm"),
            {
                "fixed_lesson_id": self.fixed_lesson.pk,
                "lesson_date": target_date.isoformat(),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_lesson"]["member_count"], 0)
