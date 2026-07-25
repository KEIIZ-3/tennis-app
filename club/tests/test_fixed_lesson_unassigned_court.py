from django.test import TestCase
from django.utils import timezone

from club.fixed_lesson_sync_facade import (
    UNASSIGNED_COURT_NAME,
    synchronize_fixed_lesson_membership,
)
from club.models import FixedLesson, Reservation, User


class FixedLessonUnassignedCourtTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.coach = User.objects.create_user(
            username="unassigned-court-coach",
            password="test-password",
            role=User.ROLE_COACH,
            full_name="コート未定担当コーチ",
            member_level=User.LEVEL_ADVANCED,
        )
        self.member = User.objects.create_user(
            username="unassigned-court-member",
            password="test-password",
            role=User.ROLE_MEMBER,
            full_name="コート未定固定会員",
            member_level=User.LEVEL_ADVANCED,
            ticket_balance=0,
        )
        self.fixed_lesson = FixedLesson.objects.create(
            title="コート未定固定レッスン",
            coach=self.coach,
            court=None,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_date=self.today,
            weekday=self.today.weekday(),
            start_hour=19,
            capacity=6,
            coach_count=1,
            court_count=1,
            weeks_ahead=2,
            is_active=True,
        )

    def test_member_add_assigns_placeholder_court_and_creates_reservations(self):
        self.fixed_lesson.members.add(self.member)
        self.fixed_lesson.refresh_from_db()

        self.assertIsNotNone(self.fixed_lesson.court_id)
        self.assertEqual(self.fixed_lesson.court.name, UNASSIGNED_COURT_NAME)

        reservations = Reservation.objects.filter(
            user=self.member,
            fixed_lesson=self.fixed_lesson,
            is_fixed_entry=True,
            status=Reservation.STATUS_ACTIVE,
        ).order_by("start_at")

        self.assertEqual(reservations.count(), 2)
        self.assertTrue(
            all(item.court_id == self.fixed_lesson.court_id for item in reservations)
        )
        self.assertTrue(all(item.availability_id is not None for item in reservations))
        self.assertTrue(all(item.ticket_consumed_at is None for item in reservations))

    def test_repeated_sync_does_not_duplicate_unassigned_court_reservations(self):
        self.fixed_lesson.members.add(self.member)

        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)
        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)

        self.assertEqual(
            Reservation.objects.filter(
                user=self.member,
                fixed_lesson=self.fixed_lesson,
                is_fixed_entry=True,
                status=Reservation.STATUS_ACTIVE,
            ).count(),
            2,
        )
