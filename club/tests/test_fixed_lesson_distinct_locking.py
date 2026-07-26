from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from club.fixed_lesson_sync_facade import (
    _locked_active_occurrence_reservations,
    synchronize_fixed_lesson_membership,
)
from club.models import Court, FixedLesson, Reservation, User


class FixedLessonDistinctLockingTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.coach = User.objects.create_user(
            username="distinct-lock-coach",
            password="test-password",
            role=User.ROLE_COACH,
            full_name="DISTINCTロック担当",
            member_level=User.LEVEL_ADVANCED,
        )
        self.member = User.objects.create_user(
            username="distinct-lock-member",
            password="test-password",
            role=User.ROLE_MEMBER,
            full_name="DISTINCTロック会員",
            member_level=User.LEVEL_ADVANCED,
            ticket_balance=0,
        )
        self.court = Court.objects.create(
            name="DISTINCTロックテストコート",
            court_type=Court.COURT_OTHER,
        )
        target_weekday = (self.today.weekday() + 1) % 7
        self.fixed_lesson = FixedLesson.objects.create(
            title="DISTINCTロック固定レッスン",
            coach=self.coach,
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_date=self.today,
            weekday=target_weekday,
            start_hour=19,
            capacity=6,
            coach_count=1,
            court_count=1,
            weeks_ahead=2,
            is_active=True,
        )

    def test_synchronization_creates_one_fixed_reservation_per_occurrence(self):
        self.fixed_lesson.members.add(self.member)
        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)
        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)

        reservations = Reservation.objects.filter(
            user=self.member,
            fixed_lesson=self.fixed_lesson,
            is_fixed_entry=True,
            status=Reservation.STATUS_ACTIVE,
        )
        self.assertEqual(reservations.count(), 2)

    def test_locked_loader_returns_each_reservation_once(self):
        self.fixed_lesson.members.add(self.member)
        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)

        reservation = Reservation.objects.filter(
            user=self.member,
            fixed_lesson=self.fixed_lesson,
            is_fixed_entry=True,
            status=Reservation.STATUS_ACTIVE,
        ).order_by("start_at").first()
        self.assertIsNotNone(reservation)

        locked = _locked_active_occurrence_reservations(
            self.fixed_lesson,
            self.member,
            reservation.availability,
            reservation.start_at,
            reservation.end_at,
        )
        self.assertEqual([item.pk for item in locked], [reservation.pk])

    def test_fixed_reservation_blocks_overlapping_normal_reservation(self):
        self.fixed_lesson.members.add(self.member)
        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)
        fixed_reservation = Reservation.objects.filter(
            user=self.member,
            fixed_lesson=self.fixed_lesson,
            is_fixed_entry=True,
            status=Reservation.STATUS_ACTIVE,
        ).first()

        duplicate = Reservation(
            user=self.member,
            coach=self.coach,
            court=self.court,
            availability=fixed_reservation.availability,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_at=fixed_reservation.start_at,
            end_at=fixed_reservation.end_at,
            status=Reservation.STATUS_ACTIVE,
        )

        with self.assertRaisesMessage(Exception, "同じ時間帯にすでに別の予約があります"):
            duplicate.full_clean()
