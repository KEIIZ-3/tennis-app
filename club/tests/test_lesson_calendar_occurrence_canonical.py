from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.models import CoachAvailability, Court, FixedLesson, Reservation, User


class LessonCalendarOccurrenceCanonicalTests(TestCase):
    def setUp(self):
        self.inoue = User.objects.create_user(
            username="canonical-inoue", role=User.ROLE_COACH, full_name="井上春佳"
        )
        self.shimizu = User.objects.create_user(
            username="canonical-shimizu", role=User.ROLE_COACH, full_name="清水峻平"
        )
        self.substitute = User.objects.create_user(
            username="canonical-substitute", role=User.ROLE_COACH, full_name="代行コーチ"
        )
        self.member = User.objects.create_user(
            username="canonical-member",
            role=User.ROLE_MEMBER,
            member_level=User.LEVEL_ADVANCED,
        )
        self.fixed_court = Court.objects.create(name="固定コート", available_court_count=3)
        self.occurrence_court = Court.objects.create(name="開催回コート", available_court_count=3)
        self.target_date = timezone.localdate() + timedelta(days=7)
        self.fixed_lesson = FixedLesson.objects.create(
            title="固定レッスン",
            coach=self.inoue,
            coach_2=self.shimizu,
            court=self.fixed_court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            target_level_2=User.LEVEL_BEGINNER_PLUS,
            start_date=self.target_date,
            weekday=self.target_date.weekday(),
            start_hour=10,
            capacity=10,
            coach_count=2,
            court_count=2,
            weeks_ahead=1,
        )

    def _calendar_row(self):
        response = self.client.get(
            reverse("club:lesson_calendar"),
            {"year": self.target_date.year, "month": self.target_date.month},
        )
        self.assertEqual(response.status_code, 200)
        return next(
            row
            for row in response.context["schedule_rows"]
            if row["fixed_lesson_id"] == str(self.fixed_lesson.pk)
        )

    def _create_linked_occurrence(self, *, coach_2=None):
        fixed_start, _fixed_end = self.fixed_lesson._build_datetimes_for_date(self.target_date)
        start_at = fixed_start + timedelta(hours=1)
        availability = CoachAvailability.objects.create(
            coach=self.inoue,
            coach_2=coach_2,
            substitute_coach=self.substitute,
            court=self.occurrence_court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_INTERMEDIATE,
            target_level_2=User.LEVEL_ADVANCED,
            start_at=start_at,
            end_at=start_at + timedelta(hours=2),
            capacity=5,
        )
        Reservation.objects.create(
            user=self.member,
            coach=self.inoue,
            substitute_coach=self.substitute,
            court=self.occurrence_court,
            availability=availability,
            fixed_lesson=self.fixed_lesson,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_INTERMEDIATE,
            start_at=availability.start_at,
            end_at=availability.end_at,
            status=Reservation.STATUS_ACTIVE,
        )
        return availability

    def test_linked_availability_is_the_complete_canonical_occurrence(self):
        availability = self._create_linked_occurrence()

        row = self._calendar_row()

        self.assertEqual(row["coach_name"], "井上春佳")
        self.assertNotIn("清水峻平", row["coach_name"])
        self.assertEqual(row["capacity"], 5)
        self.assertEqual(row["member_count"], 1)
        self.assertEqual(row["target_level_label"], availability.target_level_display_label())
        self.assertEqual(row["target_level_2"], availability.target_level_2)
        self.assertEqual(row["court_name"], self.occurrence_court.name)
        self.assertEqual(row["time_label"], "11:00〜13:00")
        self.assertEqual(row["substitute_coach_name"], "代行コーチ")

    def test_availability_second_coach_overrides_empty_fixed_second_coach(self):
        self.fixed_lesson.coach_2 = None
        self.fixed_lesson.coach_count = 1
        self.fixed_lesson.save(update_fields=["coach_2", "coach_count"])
        self._create_linked_occurrence(coach_2=self.shimizu)

        row = self._calendar_row()

        self.assertEqual(row["coach_name"], "井上春佳 / 清水峻平")

    def test_fixed_lesson_fields_remain_the_fallback_without_availability(self):
        row = self._calendar_row()

        self.assertEqual(row["coach_name"], "井上春佳 / 清水峻平")
        self.assertEqual(row["capacity"], self.fixed_lesson.effective_capacity())
        self.assertEqual(row["target_level_2"], self.fixed_lesson.target_level_2)
        self.assertEqual(row["court_name"], self.fixed_court.name)
