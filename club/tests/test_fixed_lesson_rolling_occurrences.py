from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from club.fixed_lesson_sync_facade import (
    configured_future_dates,
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
            full_name="開催範囲担当コーチ",
            member_level=User.LEVEL_ADVANCED,
        )
        self.member = User.objects.create_user(
            username="rolling-member",
            password="test-password",
            role=User.ROLE_MEMBER,
            full_name="開催範囲固定会員",
            member_level=User.LEVEL_ADVANCED,
            ticket_balance=0,
        )
        self.court = Court.objects.create(
            name="固定レッスン開催範囲テストコート",
            court_type=Court.COURT_OTHER,
        )

    def _create_fixed_lesson(self, start_date, weeks_ahead=3):
        return FixedLesson.objects.create(
            title="開催範囲を管理画面設定へ合わせる固定レッスン",
            coach=self.coach,
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_date=start_date,
            weekday=start_date.weekday(),
            start_hour=19,
            capacity=6,
            coach_count=1,
            court_count=1,
            weeks_ahead=weeks_ahead,
            is_active=True,
        )

    def test_future_dates_are_limited_to_configured_occurrences(self):
        start_date = self.today + timedelta(days=1)
        fixed_lesson = self._create_fixed_lesson(start_date, weeks_ahead=3)

        target_dates = configured_future_dates(fixed_lesson, self.today)

        self.assertEqual(
            target_dates,
            [
                start_date,
                start_date + timedelta(days=7),
                start_date + timedelta(days=14),
            ],
        )
        self.assertNotIn(start_date + timedelta(days=21), target_dates)

    def test_expired_configured_series_does_not_create_new_rolling_reservations(self):
        fixed_lesson = self._create_fixed_lesson(
            self.today - timedelta(days=70),
            weeks_ahead=3,
        )
        fixed_lesson.members.add(self.member)

        synchronize_fixed_lesson_membership(fixed_lesson.pk)

        self.assertEqual(configured_future_dates(fixed_lesson, self.today), [])
        self.assertFalse(
            Reservation.objects.filter(
                user=self.member,
                fixed_lesson=fixed_lesson,
                is_fixed_entry=True,
                status=Reservation.STATUS_ACTIVE,
            ).exists()
        )
