from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from club import fixed_lesson_membership_service
from club.fixed_lesson_integrity_service import (
    configured_future_dates,
    synchronize_fixed_lesson_membership,
)
from club.models import Court, FixedLesson, Reservation, User


class FixedLessonIntegrityAuditTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.coach = User.objects.create_user(
            username="integrity-audit-coach",
            password="test-password",
            role=User.ROLE_COACH,
            full_name="整合性監査コーチ",
            member_level=User.LEVEL_ADVANCED,
        )
        self.member = User.objects.create_user(
            username="integrity-audit-member",
            password="test-password",
            role=User.ROLE_MEMBER,
            full_name="整合性監査会員",
            member_level=User.LEVEL_BEGINNER,
            ticket_balance=0,
        )
        self.court = Court.objects.create(
            name="整合性監査コート",
            court_type=Court.COURT_OTHER,
        )
        start_date = self.today + timedelta(days=1)
        self.fixed_lesson = FixedLesson.objects.create(
            title="整合性監査固定レッスン",
            coach=self.coach,
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_date=start_date,
            weekday=start_date.weekday(),
            start_hour=19,
            capacity=5,
            coach_count=1,
            court_count=1,
            weeks_ahead=4,
            is_active=True,
        )

    def test_configured_occurrences_do_not_roll_beyond_admin_range(self):
        configured = self.fixed_lesson.scheduled_occurrence_dates()
        self.assertEqual(
            configured_future_dates(self.fixed_lesson, self.today),
            configured,
        )

        after_first_occurrence = configured[0] + timedelta(days=1)
        self.assertEqual(
            configured_future_dates(self.fixed_lesson, after_first_occurrence),
            configured[1:],
        )
        self.assertNotIn(
            configured[-1] + timedelta(days=7),
            configured_future_dates(self.fixed_lesson, after_first_occurrence),
        )

    def test_sync_does_not_replace_global_membership_loader(self):
        original_loader = fixed_lesson_membership_service._active_occurrence_reservations
        self.fixed_lesson.members.add(self.member)

        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)

        self.assertIs(
            fixed_lesson_membership_service._active_occurrence_reservations,
            original_loader,
        )
        self.assertEqual(
            Reservation.objects.filter(
                fixed_lesson=self.fixed_lesson,
                user=self.member,
                is_fixed_entry=True,
                status=Reservation.STATUS_ACTIVE,
            ).count(),
            4,
        )

    def test_reservation_beyond_configured_range_is_canceled(self):
        self.fixed_lesson.members.add(self.member)
        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)

        configured = self.fixed_lesson.scheduled_occurrence_dates()
        start_at, end_at = self.fixed_lesson._build_datetimes_for_date(
            configured[-1] + timedelta(days=7)
        )
        extra = Reservation(
            user=self.member,
            coach=self.coach,
            court=self.court,
            fixed_lesson=self.fixed_lesson,
            is_fixed_entry=True,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=end_at,
            status=Reservation.STATUS_ACTIVE,
        )
        Reservation.objects.bulk_create([extra])

        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)

        extra.refresh_from_db()
        self.assertEqual(extra.status, Reservation.STATUS_CANCELED)
        self.assertEqual(
            extra.cancellation_reason,
            "固定レッスンの予約生成期間変更による自動整理",
        )
