from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from club.models import CoachAvailability, Court, LessonWaitlist, Reservation
from club.notification_service import resolve_lesson_notification_context
from club.notifications import (
    build_reservation_created_message,
    build_waitlist_registered_for_member_email_message,
)


class LessonNotificationContextTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.member = user_model.objects.create_user(
            username="notice-member", member_level=user_model.LEVEL_INTERMEDIATE
        )
        self.coach_a = user_model.objects.create_user(
            username="notice-a", full_name="コーチA", role=user_model.ROLE_COACH
        )
        self.coach_b = user_model.objects.create_user(
            username="notice-b", full_name="コーチB", role=user_model.ROLE_COACH
        )
        self.substitute = user_model.objects.create_user(
            username="notice-sub", full_name="代行C", role=user_model.ROLE_COACH
        )
        self.old_court = Court.objects.create(name="旧コート")
        self.current_court = Court.objects.create(name="開催回コート")
        lesson_date = timezone.localdate() + timedelta(days=7)
        self.start_at = timezone.make_aware(
            datetime.combine(lesson_date, time(10))
        )
        self.availability = CoachAvailability.objects.create(
            coach=self.coach_a,
            coach_2=self.coach_b,
            court=self.current_court,
            lesson_type=Reservation.LESSON_GROUP,
            target_level=user_model.LEVEL_INTERMEDIATE,
            start_at=self.start_at,
            end_at=self.start_at + timedelta(hours=2),
            capacity=6,
        )

    def reservation(self, **overrides):
        values = {
            "user": self.member,
            "coach": self.coach_a,
            "court": self.current_court,
            "availability": self.availability,
            "lesson_type": Reservation.LESSON_GROUP,
            "target_level": get_user_model().LEVEL_INTERMEDIATE,
            "start_at": self.start_at,
            "end_at": self.start_at + timedelta(hours=2),
        }
        reservation = Reservation.objects.create(**values)
        stale_values = {
            "coach": self.coach_b,
            "court": self.old_court,
            "lesson_type": Reservation.LESSON_GENERAL,
            "target_level": get_user_model().LEVEL_BEGINNER,
        }
        stale_values.update(overrides)
        Reservation.objects.filter(pk=reservation.pk).update(**stale_values)
        return Reservation.objects.select_related("availability").get(pk=reservation.pk)

    def test_availability_is_canonical_for_two_coaches_court_type_and_datetime(self):
        message = build_reservation_created_message(self.reservation())

        self.assertIn("コーチ: コーチA / コーチB", message)
        self.assertIn("種別: グループ", message)
        self.assertIn("コート: 開催回コート", message)
        self.assertIn(timezone.localtime(self.start_at).strftime("%Y-%m-%d %H:%M"), message)
        self.assertNotIn("旧コート", message)

    def test_occurrence_coach_override_does_not_fall_back_to_stale_second_coach(self):
        self.availability.coach_2 = None
        self.availability.coach_assignment_overridden = True
        self.availability.save(update_fields=["coach_2", "coach_assignment_overridden"])

        context = resolve_lesson_notification_context(self.reservation())

        self.assertEqual(context.display_coaches, (self.coach_a,))

    def test_substitute_is_the_only_actual_display_coach(self):
        self.availability.substitute_coach = self.substitute
        self.availability.save(update_fields=["substitute_coach"])

        context = resolve_lesson_notification_context(self.reservation())

        self.assertEqual(context.assigned_coach, self.substitute)
        self.assertEqual(context.display_coaches, (self.substitute,))

    def test_reservation_without_availability_keeps_snapshot_contract(self):
        reservation = self.reservation(availability=None, substitute_coach=self.substitute)

        context = resolve_lesson_notification_context(reservation)

        self.assertEqual(context.assigned_coach, self.substitute)
        self.assertEqual(context.court, self.old_court)
        self.assertEqual(context.lesson_type, Reservation.LESSON_GENERAL)

    def test_waitlist_message_uses_canonical_occurrence(self):
        waitlist = LessonWaitlist.objects.create(
            user=self.member,
            coach=self.coach_b,
            court=self.old_court,
            availability=self.availability,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=get_user_model().LEVEL_BEGINNER,
            start_at=self.start_at - timedelta(hours=1),
            end_at=self.start_at,
        )

        message = build_waitlist_registered_for_member_email_message(waitlist)

        self.assertIn("コーチ: コーチA / コーチB", message)
        self.assertIn("種別: グループ", message)
        self.assertIn("コート: 開催回コート", message)
        self.assertNotIn("旧コート", message)

    def test_canceled_reservation_keeps_occurrence_consistent_while_link_exists(self):
        reservation = self.reservation(status=Reservation.STATUS_CANCELED)

        message = build_reservation_created_message(reservation)

        self.assertIn("コーチ: コーチA / コーチB", message)
        self.assertIn("コート: 開催回コート", message)
