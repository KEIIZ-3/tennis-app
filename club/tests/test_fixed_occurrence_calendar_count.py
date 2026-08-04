from django.test import TestCase
from django.utils import timezone

from club.customer_ui import _replace_fixed_occurrence_counts
from club.fixed_occurrence_participants import active_count_for_occurrence
from club.models import Court, FixedLesson, Reservation, User


class FixedOccurrenceCalendarCountTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(
            username="calendar-count-coach",
            password="test-password",
            role=User.ROLE_COACH,
            full_name="カレンダー人数担当",
            member_level=User.LEVEL_ADVANCED,
        )
        self.member = User.objects.create_user(
            username="calendar-count-member",
            password="test-password",
            role=User.ROLE_MEMBER,
            full_name="カレンダー人数会員",
            member_level=User.LEVEL_BEGINNER,
        )
        self.court = Court.objects.create(
            name="カレンダー人数コート",
            court_type=Court.COURT_OTHER,
        )
        today = timezone.localdate()
        self.target_date = today
        self.fixed_lesson = FixedLesson.objects.create(
            title="カレンダー人数固定レッスン",
            coach=self.coach,
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_date=today,
            weekday=today.weekday(),
            start_hour=19,
            capacity=5,
            coach_count=1,
            court_count=1,
            weeks_ahead=1,
            is_active=True,
        )
        start_at, end_at = self.fixed_lesson._build_datetimes_for_date(today)
        self.reservation = Reservation(
            user=self.member,
            coach=self.coach,
            court=self.court,
            fixed_lesson=self.fixed_lesson,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=end_at,
            status=Reservation.STATUS_ACTIVE,
            is_fixed_entry=True,
        )
        Reservation.objects.bulk_create([self.reservation])

    def test_cancelled_reservation_is_not_counted(self):
        self.assertEqual(
            active_count_for_occurrence(self.fixed_lesson, self.target_date),
            1,
        )
        self.reservation.status = Reservation.STATUS_CANCELED
        self.reservation.save(update_fields=["status"])
        self.assertEqual(
            active_count_for_occurrence(self.fixed_lesson, self.target_date),
            0,
        )

    def test_calendar_html_uses_occurrence_count_instead_of_physical_slot_count(self):
        lesson_date = self.target_date.isoformat()
        document = (
            '<a data-member-list-url="/lesson-calendar/members/?fixed_lesson_id='
            f'{self.fixed_lesson.pk}&amp;lesson_date={lesson_date}">'
            '<div class="event-meta">4/5名</div></a>'
        )
        updated = _replace_fixed_occurrence_counts(
            document,
            {(str(self.fixed_lesson.pk), lesson_date): 3},
        )
        self.assertIn('<div class="event-meta">3/5名</div>', updated)
        self.assertNotIn('<div class="event-meta">4/5名</div>', updated)
