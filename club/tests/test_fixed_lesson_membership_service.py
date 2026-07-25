from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from club.models import Court, FixedLesson, Reservation, User


class FixedLessonMembershipServiceTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(
            username="fixed-coach",
            password="test-password",
            role=User.ROLE_COACH,
            full_name="固定担当コーチ",
            member_level=User.LEVEL_ADVANCED,
        )
        self.member = User.objects.create_user(
            username="fixed-member",
            password="test-password",
            role=User.ROLE_MEMBER,
            full_name="固定参加会員",
            member_level=User.LEVEL_ADVANCED,
            ticket_balance=0,
        )
        self.court = Court.objects.create(
            name="固定レッスンテストコート",
            court_type=Court.COURT_OTHER,
        )

        start_date = timezone.localdate() + timedelta(days=1)
        self.fixed_lesson = FixedLesson.objects.create(
            title="固定メンバー同期テスト",
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
            weeks_ahead=3,
            is_active=True,
        )

    def test_zero_ticket_member_gets_all_future_fixed_reservations(self):
        self.fixed_lesson.members.add(self.member)

        reservations = Reservation.objects.filter(
            user=self.member,
            fixed_lesson=self.fixed_lesson,
            is_fixed_entry=True,
            status=Reservation.STATUS_ACTIVE,
        ).order_by("start_at")

        self.assertEqual(reservations.count(), 3)
        self.assertTrue(all(item.ticket_consumed_at is None for item in reservations))
        self.member.refresh_from_db()
        self.assertEqual(self.member.ticket_balance, 0)

    def test_removing_member_cancels_future_fixed_reservations(self):
        self.fixed_lesson.members.add(self.member)
        self.fixed_lesson.members.remove(self.member)

        self.assertFalse(
            Reservation.objects.filter(
                user=self.member,
                fixed_lesson=self.fixed_lesson,
                status=Reservation.STATUS_ACTIVE,
            ).exists()
        )
        self.assertEqual(
            Reservation.objects.filter(
                user=self.member,
                fixed_lesson=self.fixed_lesson,
                status=Reservation.STATUS_CANCELED,
                cancellation_reason="固定レッスンメンバー解除",
            ).count(),
            3,
        )

    def test_member_addition_is_idempotent(self):
        self.fixed_lesson.members.add(self.member)
        self.fixed_lesson.members.add(self.member)

        self.assertEqual(
            Reservation.objects.filter(
                user=self.member,
                fixed_lesson=self.fixed_lesson,
                is_fixed_entry=True,
                status=Reservation.STATUS_ACTIVE,
            ).count(),
            3,
        )
