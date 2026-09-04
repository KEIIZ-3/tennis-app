from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from club.models import (
    CoachAvailability,
    Court,
    FixedLesson,
    Reservation,
    ReservationParticipant,
)


class ReservationDetailDisplayTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.member = user_model.objects.create_user(
            username="detail-member",
            password="password12345",
            full_name="Detail Member",
            role=user_model.ROLE_MEMBER,
            member_level=user_model.LEVEL_BEGINNER,
            is_profile_completed=True,
        )
        self.coach = user_model.objects.create_user(
            username="detail-coach",
            password="password12345",
            full_name="Detail Coach",
            role=user_model.ROLE_COACH,
            member_level=user_model.LEVEL_BEGINNER,
            is_profile_completed=True,
        )
        self.substitute = user_model.objects.create_user(
            username="detail-substitute",
            password="password12345",
            full_name="Substitute Coach",
            role=user_model.ROLE_COACH,
            member_level=user_model.LEVEL_BEGINNER,
            is_profile_completed=True,
        )
        self.court = Court.objects.create(
            name="Detail Court",
            is_active=True,
            court_type=Court.COURT_SONO,
        )
        lesson_date = timezone.localdate() + timedelta(days=10)
        self.start_at = timezone.make_aware(datetime.combine(lesson_date, time(19, 0)))
        self.availability = CoachAvailability.objects.create(
            coach=self.coach,
            substitute_coach=self.substitute,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=user_model.LEVEL_BEGINNER,
            start_at=self.start_at,
            end_at=self.start_at + timedelta(hours=2),
            capacity=6,
            status=CoachAvailability.STATUS_OPEN,
        )
        self.fixed_lesson = FixedLesson.objects.create(
            title="Detail Lesson",
            coach=self.coach,
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=user_model.LEVEL_BEGINNER,
            start_date=lesson_date,
            weekday=lesson_date.weekday(),
            start_hour=19,
            capacity=6,
            is_active=True,
        )

    def _reservation(self, *, status=Reservation.STATUS_ACTIVE):
        return Reservation.objects.create(
            user=self.member,
            coach=self.coach,
            substitute_coach=self.substitute,
            court=self.court,
            availability=self.availability,
            fixed_lesson=self.fixed_lesson,
            is_fixed_entry=True,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=self.member.member_level,
            start_at=self.start_at,
            end_at=self.start_at + timedelta(hours=2),
            status=status,
        )

    def test_detail_context_preserves_snapshot_and_related_display_data(self):
        reservation = self._reservation(status=Reservation.STATUS_PENDING)
        ReservationParticipant.objects.create(
            reservation=reservation,
            parent=self.member,
            participant_type="family",
            participant_name="Snapshot Child",
            participant_level=self.member.member_level,
            participant_level_label="Snapshot Level",
            relationship_label="Child",
        )
        self.client.force_login(self.member)

        response = self.client.get(reverse("club:reservation_detail", args=[reservation.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["reservation"].pk, reservation.pk)
        self.assertEqual(response.context["participant"]["name"], "Snapshot Child")
        self.assertEqual(response.context["participant"]["relationship_label"], "Child")
        self.assertTrue(response.context["participant"]["is_family"])
        self.assertEqual(response.context["assigned_coach_name"], self.substitute.display_name())
        self.assertEqual(
            response.context["slot_capacity"],
            self.availability.effective_capacity(),
        )
        self.assertEqual(response.context["slot_active_count"], 0)
        self.assertEqual(response.context["same_slot_reservation_rows"], [])

    def test_representative_detail_get_keeps_pre_extraction_query_count(self):
        reservation = self._reservation()
        self.client.force_login(self.member)

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("club:reservation_detail", args=[reservation.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(queries), 15)
