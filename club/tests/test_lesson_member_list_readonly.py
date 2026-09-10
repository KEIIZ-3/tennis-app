from datetime import date, datetime, timedelta

from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from club import lesson_execution
from club.fixed_lesson_occurrence_service import (
    resolve_fixed_lesson_availability_readonly,
)
from club.models import CoachAvailability, Court, FixedLesson, Reservation, User
from club.settlement_models import MonthlySettlement


class LessonMemberListReadonlyTests(TransactionTestCase):
    def setUp(self):
        self.coach = User.objects.create_user(
            username="readonly-coach", role=User.ROLE_COACH
        )
        self.coach_2 = User.objects.create_user(
            username="readonly-coach-2", role=User.ROLE_COACH
        )
        self.court = Court.objects.create(name="readonly-court")
        self.client.force_login(self.coach)

    def _occurrence(self, *, label, lesson_date, coach_2=None, overridden=False):
        fixed_lesson = FixedLesson.objects.create(
            title=label,
            coach=self.coach,
            coach_2=coach_2,
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            start_date=lesson_date,
            weekday=lesson_date.weekday(),
            start_hour=10,
            capacity=6,
            coach_count=2 if coach_2 else 1,
            court_count=1,
            weeks_ahead=1,
        )
        start_at, end_at = fixed_lesson._build_datetimes_for_date(lesson_date)
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            coach_2=None if overridden else coach_2,
            court=self.court,
            lesson_type=fixed_lesson.lesson_type,
            start_at=start_at,
            end_at=end_at,
            capacity=6,
            coach_count=1 if overridden else fixed_lesson.coach_count,
            court_count=1,
            fixed_lesson_source=fixed_lesson,
            coach_assignment_overridden=overridden,
        )
        return fixed_lesson, availability, start_at, end_at

    def _get_members(self, fixed_lesson, availability):
        lesson_date = fixed_lesson.start_date
        return self.client.get(
            reverse("club:lesson_calendar_member_list"),
            {
                "availability_id": availability.pk,
                "fixed_lesson_id": fixed_lesson.pk,
                "lesson_date": lesson_date.isoformat(),
                "year": lesson_date.year,
                "month": lesson_date.month,
            },
        )

    def test_production_fixed_lesson_shapes_return_200_without_writes(self):
        cases = (
            self._occurrence(label="availability-71-fixed-55", lesson_date=date(2026, 9, 24)),
            self._occurrence(
                label="availability-74-fixed-59",
                lesson_date=date(2026, 9, 30),
                coach_2=self.coach_2,
            ),
        )
        before_count = CoachAvailability.objects.count()
        before_settlements = MonthlySettlement.objects.count()
        fields = (
            "pk",
            "coach_id",
            "coach_2_id",
            "fixed_lesson_source_id",
            "coach_assignment_overridden",
        )
        before_values = list(CoachAvailability.objects.order_by("pk").values(*fields))

        for fixed_lesson, availability, _start_at, _end_at in cases:
            self.assertEqual(self._get_members(fixed_lesson, availability).status_code, 200)

        self.assertEqual(CoachAvailability.objects.count(), before_count)
        self.assertEqual(MonthlySettlement.objects.count(), before_settlements)
        self.assertEqual(
            list(CoachAvailability.objects.order_by("pk").values(*fields)),
            before_values,
        )

    def test_readonly_resolver_runs_outside_atomic_and_preserves_override(self):
        fixed_lesson, availability, start_at, end_at = self._occurrence(
            label="override-9-29",
            lesson_date=date(2026, 9, 29),
            coach_2=self.coach_2,
            overridden=True,
        )

        resolved = resolve_fixed_lesson_availability_readonly(
            fixed_lesson, start_at, end_at
        )

        self.assertEqual(resolved.pk, availability.pk)
        availability.refresh_from_db()
        self.assertIsNone(availability.coach_2_id)
        self.assertTrue(availability.coach_assignment_overridden)

    def test_single_lesson_card_and_status_lookup_are_readonly(self):
        lesson_date = date(2026, 9, 30)
        starts = timezone.make_aware(datetime.combine(lesson_date, datetime.min.time()))
        starts += timedelta(hours=15)
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            start_at=starts,
            end_at=starts + timedelta(hours=2),
            capacity=4,
        )
        before_count = CoachAvailability.objects.count()

        response = self.client.get(
            reverse("club:lesson_calendar_member_list"),
            {"availability_id": availability.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(CoachAvailability.objects.count(), before_count)
        self.assertFalse(MonthlySettlement.objects.exists())
        self.assertIn(
            availability.pk,
            lesson_execution.status_by_availability(self.coach, {(2026, 9)}),
        )
