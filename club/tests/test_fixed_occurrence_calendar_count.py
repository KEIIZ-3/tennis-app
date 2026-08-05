from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

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

    def test_calendar_html_uses_occurrence_count_without_response_rewrite(self):
        response = self.client.get(
            reverse("club:lesson_calendar"),
            {"year": self.target_date.year, "month": self.target_date.month},
        )

        self.assertContains(response, '<div class="event-meta">1/5名</div>', html=True)

    def test_calendar_does_not_mix_unrelated_reservation_in_same_physical_slot(self):
        other_member = User.objects.create_user(
            username="calendar-count-other",
            password="test-password",
            role=User.ROLE_MEMBER,
            full_name="別レッスン会員",
            member_level=User.LEVEL_BEGINNER,
        )
        start_at, end_at = self.fixed_lesson._build_datetimes_for_date(
            self.target_date,
        )
        Reservation.objects.bulk_create(
            [
                Reservation(
                    user=other_member,
                    coach=self.coach,
                    court=self.court,
                    lesson_type=Reservation.LESSON_GENERAL,
                    target_level=User.LEVEL_BEGINNER,
                    start_at=start_at,
                    end_at=end_at,
                    status=Reservation.STATUS_ACTIVE,
                )
            ]
        )

        response = self.client.get(
            reverse("club:lesson_calendar"),
            {"year": self.target_date.year, "month": self.target_date.month},
        )

        fixed_row = next(
            row
            for row in response.context["schedule_rows"]
            if row["fixed_lesson_id"] == str(self.fixed_lesson.pk)
            and row["lesson_date"] == self.target_date.isoformat()
        )
        self.assertEqual(fixed_row["member_count"], 1)

    def test_fixed_lesson_confirmation_uses_linked_reservations_only(self):
        other_member = User.objects.create_user(
            username="confirmation-count-other",
            password="test-password",
            role=User.ROLE_MEMBER,
            full_name="確認別レッスン会員",
            member_level=User.LEVEL_BEGINNER,
        )
        start_at, end_at = self.fixed_lesson._build_datetimes_for_date(
            self.target_date,
        )
        Reservation.objects.bulk_create(
            [
                Reservation(
                    user=other_member,
                    coach=self.coach,
                    court=self.court,
                    lesson_type=Reservation.LESSON_GENERAL,
                    target_level=User.LEVEL_BEGINNER,
                    start_at=start_at,
                    end_at=end_at,
                    status=Reservation.STATUS_ACTIVE,
                )
            ]
        )
        self.member.email = "calendar-member@example.com"
        self.member.phone_number = "09012345678"
        self.member.is_profile_completed = True
        self.member.save(
            update_fields=["email", "phone_number", "is_profile_completed"],
        )
        self.client.force_login(self.member)

        response = self.client.get(
            reverse("club:reservation_create"),
            {
                "fixed_lesson_id": self.fixed_lesson.pk,
                "lesson_date": self.target_date.isoformat(),
                "year": self.target_date.year,
                "month": self.target_date.month,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_lesson"]["member_count"], 1)
