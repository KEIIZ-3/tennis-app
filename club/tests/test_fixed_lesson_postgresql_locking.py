from django.db import connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from club.fixed_lesson_sync_facade import synchronize_fixed_lesson_membership
from club.models import FixedLesson, Reservation, User


class FixedLessonPostgreSQLLockingTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(
            username="postgres-lock-coach",
            password="test-password",
            role=User.ROLE_COACH,
            full_name="PostgreSQLロック確認コーチ",
            member_level=User.LEVEL_ADVANCED,
        )
        self.member = User.objects.create_user(
            username="postgres-lock-member",
            password="test-password",
            role=User.ROLE_MEMBER,
            full_name="PostgreSQLロック確認会員",
            member_level=User.LEVEL_BEGINNER,
            ticket_balance=0,
        )
        self.fixed_lesson = FixedLesson.objects.create(
            title="コート未定ロック確認",
            coach=self.coach,
            court=None,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_date=timezone.localdate(),
            weekday=timezone.localdate().weekday(),
            start_hour=19,
            capacity=6,
            coach_count=1,
            court_count=1,
            weeks_ahead=2,
            is_active=True,
        )

    def test_nullable_relations_are_not_joined_into_locked_fixed_lesson_query(self):
        with CaptureQueriesContext(connection) as captured:
            with transaction.atomic():
                synchronize_fixed_lesson_membership(self.fixed_lesson.pk)

        fixed_lesson_selects = [
            item["sql"].upper()
            for item in captured.captured_queries
            if "FROM \"CLUB_FIXEDLESSON\"" in item["sql"].upper()
            and item["sql"].lstrip().upper().startswith("SELECT")
        ]
        self.assertTrue(fixed_lesson_selects)
        self.assertTrue(
            all("LEFT OUTER JOIN" not in sql for sql in fixed_lesson_selects)
        )

    def test_courtless_fixed_member_reservations_are_created_once(self):
        self.fixed_lesson.members.add(self.member)

        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)
        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)

        self.fixed_lesson.refresh_from_db()
        self.assertIsNotNone(self.fixed_lesson.court_id)
        self.assertEqual(
            Reservation.objects.filter(
                user=self.member,
                fixed_lesson=self.fixed_lesson,
                is_fixed_entry=True,
                status=Reservation.STATUS_ACTIVE,
            ).count(),
            2,
        )
