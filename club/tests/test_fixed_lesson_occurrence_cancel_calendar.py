from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.fixed_lesson_sync_facade import synchronize_fixed_lesson_membership
from club.court_number_line_notice import _slot_participants
from club.family_reservations import resolve_reservation_participant, save_reservation_participant_snapshot
from club.fixed_occurrence_participants import active_count_for_occurrence
from club.lesson_participants import reservations_for_object
from club.models import FixedLesson, Reservation, ReservationParticipant, User


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
            capacity=3,
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

    def test_fixed_and_regular_participants_stay_consistent_for_every_consumer(self):
        fixed_reservation = Reservation.objects.filter(
            user=self.member,
            fixed_lesson=self.fixed_lesson,
            status=Reservation.STATUS_ACTIVE,
        ).order_by("start_at").first()
        regular_member = User.objects.create_user(
            username="occurrence-regular-member",
            password="test-password",
            role=User.ROLE_MEMBER,
            full_name="通常予約会員",
            member_level=User.LEVEL_BEGINNER,
            is_profile_completed=True,
        )
        regular_reservation = Reservation.objects.create(
            user=regular_member,
            coach=fixed_reservation.coach,
            court=fixed_reservation.court,
            availability=fixed_reservation.availability,
            fixed_lesson=self.fixed_lesson,
            lesson_type=fixed_reservation.lesson_type,
            target_level=fixed_reservation.target_level,
            start_at=fixed_reservation.start_at,
            end_at=fixed_reservation.end_at,
            status=Reservation.STATUS_ACTIVE,
        )
        save_reservation_participant_snapshot(
            regular_reservation,
            resolve_reservation_participant(regular_member, "self"),
        )
        target_date = timezone.localtime(fixed_reservation.start_at).date()

        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)
        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)

        expected_ids = {fixed_reservation.pk, regular_reservation.pk}
        self.assertSetEqual(
            set(reservations_for_object(fixed_reservation).values_list("pk", flat=True)),
            expected_ids,
        )
        self.assertSetEqual(
            set(_slot_participants(fixed_reservation).values_list("pk", flat=True)),
            expected_ids,
        )
        self.assertEqual(fixed_reservation.active_count_in_same_slot(), 2)
        self.assertEqual(active_count_for_occurrence(self.fixed_lesson, target_date), 2)
        self.assertEqual(
            ReservationParticipant.objects.filter(reservation_id__in=expected_ids).count(),
            2,
        )

        confirm_response = self.client.get(
            reverse("club:lesson_reservation_confirm"),
            {
                "fixed_lesson_id": self.fixed_lesson.pk,
                "lesson_date": target_date.isoformat(),
            },
        )
        self.assertEqual(confirm_response.status_code, 200)
        self.assertEqual(confirm_response.context["selected_lesson"]["member_count"], 2)

        self.client.force_login(self.coach)
        card_response = self.client.get(
            reverse("club:lesson_calendar_member_list"),
            {
                "fixed_lesson_id": self.fixed_lesson.pk,
                "lesson_date": target_date.isoformat(),
            },
        )
        self.assertEqual(card_response.status_code, 200)
        self.assertEqual(card_response.context["active_count"], 2)

        fixed_reservation.cancel(
            created_by=self.member,
            reason="会員が予約確認画面からキャンセル",
        )
        remaining_ids = {regular_reservation.pk}
        self.assertSetEqual(
            set(reservations_for_object(fixed_reservation).values_list("pk", flat=True)),
            remaining_ids,
        )
        self.assertSetEqual(
            set(_slot_participants(fixed_reservation).values_list("pk", flat=True)),
            remaining_ids,
        )
        self.assertEqual(active_count_for_occurrence(self.fixed_lesson, target_date), 1)
