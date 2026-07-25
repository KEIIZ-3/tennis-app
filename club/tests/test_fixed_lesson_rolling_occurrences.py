from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from club.fixed_lesson_membership_service import (
    _rolling_target_dates,
    synchronize_fixed_lesson_membership,
)
from club.models import Court, FixedLesson, Reservation, User


class FixedLessonRollingOccurrenceTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.coach = User.objects.create_user(
            username="rolling-coach",
            password="test-password",
            role=User.ROLE_COACH,
            full_name="ローリング担当コーチ",
            member_level=User.LEVEL_ADVANCED,
        )
        self.member = User.objects.create_user(
            username="rolling-member",
            password="test-password",
            role=User.ROLE_MEMBER,
            full_name="ローリング固定会員",
            member_level=User.LEVEL_ADVANCED,
            ticket_balance=0,
        )
        self.court = Court.objects.create(
            name="ローリング固定レッスンテストコート",
            court_type=Court.COURT_OTHER,
        )

        target_weekday = (self.today.weekday() + 1) % 7
        self.fixed_lesson = FixedLesson.objects.create(
            title="過去開始日の固定レッスン",
            coach=self.coach,
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_date=self.today - timedelta(days=70),
            weekday=target_weekday,
            start_hour=19,
            capacity=6,
            coach_count=1,
            court_count=1,
            weeks_ahead=3,
            is_active=True,
        )

    def test_rolling_target_dates_start_from_today_or_later(self):
        target_dates = _rolling_target_dates(self.fixed_lesson, self.today)

        self.assertEqual(len(target_dates), 3)
        self.assertTrue(all(target_date >= self.today for target_date in target_dates))
        self.assertTrue(
            all(target_date.weekday() == self.fixed_lesson.weekday for target_date in target_dates)
        )
        self.assertEqual(target_dates[1] - target_dates[0], timedelta(days=7))
        self.assertEqual(target_dates[2] - target_dates[1], timedelta(days=7))

    def test_past_start_date_still_creates_future_fixed_reservations(self):
        self.fixed_lesson.members.add(self.member)
        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)

        target_dates = _rolling_target_dates(self.fixed_lesson, self.today)
        reservations = Reservation.objects.filter(
            user=self.member,
            fixed_lesson=self.fixed_lesson,
            is_fixed_entry=True,
            status=Reservation.STATUS_ACTIVE,
        ).order_by("start_at")

        self.assertEqual(reservations.count(), 3)
        self.assertEqual(
            [timezone.localtime(item.start_at).date() for item in reservations],
            target_dates,
        )
        self.assertTrue(all(item.ticket_consumed_at is None for item in reservations))
