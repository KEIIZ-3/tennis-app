from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from club.fixed_occurrence_participants import active_count_for_occurrence
from club.fixed_lesson_sync_facade import synchronize_fixed_lesson_membership
from club.lesson_participants import (
    CAPACITY_CONSUMING_STATUSES,
    CANCELED_RESERVATION_STATUSES,
    CONFIRMED_PARTICIPANT_STATUSES,
    reservations_for_fixed_occurrence,
)
from club.models import CoachAvailability, Court, FixedLesson, Reservation, User


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

    def test_canonical_status_sets_keep_participation_and_capacity_distinct(self):
        self.assertEqual(CONFIRMED_PARTICIPANT_STATUSES, (Reservation.STATUS_ACTIVE,))
        self.assertEqual(
            CAPACITY_CONSUMING_STATUSES,
            (Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING),
        )
        self.assertEqual(
            CANCELED_RESERVATION_STATUSES,
            (Reservation.STATUS_CANCELED, Reservation.STATUS_RAIN_CANCELED),
        )

        pending_member = User.objects.create_user(
            username="calendar-pending-member",
            role=User.ROLE_MEMBER,
        )
        pending = Reservation(
            user=pending_member,
            coach=self.coach,
            court=self.court,
            fixed_lesson=self.fixed_lesson,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_at=self.reservation.start_at,
            end_at=self.reservation.end_at,
            status=Reservation.STATUS_PENDING,
        )
        Reservation.objects.bulk_create([pending])
        confirmed = reservations_for_fixed_occurrence(
            self.fixed_lesson, self.target_date
        )
        capacity_consuming = reservations_for_fixed_occurrence(
            self.fixed_lesson,
            self.target_date,
            statuses=CAPACITY_CONSUMING_STATUSES,
        )
        self.assertEqual(list(confirmed.values_list("pk", flat=True)), [self.reservation.pk])
        self.assertEqual(
            set(capacity_consuming.values_list("pk", flat=True)),
            {self.reservation.pk, pending.pk},
        )

    def test_rain_canceled_reservation_is_not_counted(self):
        Reservation.objects.filter(pk=self.reservation.pk).update(
            status=Reservation.STATUS_RAIN_CANCELED
        )
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

    def test_calendar_query_count_does_not_scale_with_fixed_occurrences(self):
        url = reverse("club:lesson_calendar")
        params = {"year": self.target_date.year, "month": self.target_date.month}
        with CaptureQueriesContext(connection) as single_context:
            self.client.get(url, params)

        self.fixed_lesson.weeks_ahead = 4
        self.fixed_lesson.save(update_fields=["weeks_ahead"])
        extra_availabilities = []
        extra_reservations = []
        for target_date in self.fixed_lesson.configured_occurrence_dates()[1:]:
            start_at, end_at = self.fixed_lesson._build_datetimes_for_date(target_date)
            availability = CoachAvailability(
                coach=self.coach,
                court=self.court,
                lesson_type=Reservation.LESSON_GENERAL,
                target_level=User.LEVEL_BEGINNER,
                start_at=start_at,
                end_at=end_at,
                capacity=5,
            )
            extra_availabilities.append(availability)
        CoachAvailability.objects.bulk_create(extra_availabilities)
        for availability in extra_availabilities:
            extra_reservations.append(Reservation(
                user=self.member,
                coach=self.coach,
                court=self.court,
                availability=availability,
                fixed_lesson=self.fixed_lesson,
                lesson_type=Reservation.LESSON_GENERAL,
                target_level=User.LEVEL_BEGINNER,
                start_at=availability.start_at,
                end_at=availability.end_at,
                status=Reservation.STATUS_ACTIVE,
                is_fixed_entry=True,
            ))
        Reservation.objects.bulk_create(extra_reservations)

        with CaptureQueriesContext(connection) as multiple_context:
            response = self.client.get(url, params)

        displayed_occurrences = [
            row for row in response.context["schedule_rows"]
            if row["fixed_lesson_id"] == str(self.fixed_lesson.pk)
        ]
        self.assertEqual(len(displayed_occurrences), 4)
        self.assertLessEqual(len(multiple_context), len(single_context) + 1)

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

    def test_recreated_fixed_lesson_rebinds_shared_occurrence_without_ticket_side_effects(self):
        start_at, end_at = self.fixed_lesson._build_datetimes_for_date(self.target_date)
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=end_at,
            capacity=5,
        )
        Reservation.objects.filter(pk=self.reservation.pk).update(
            availability=availability,
            tickets_used=1,
            ticket_consumed_at=timezone.now(),
            is_fixed_entry=False,
        )
        replacement_member = User.objects.create_user(
            username="replacement-fixed-member",
            role=User.ROLE_MEMBER,
            full_name="再作成後会員",
            member_level=User.LEVEL_BEGINNER,
        )
        replacement = FixedLesson.objects.create(
            title=self.fixed_lesson.title,
            coach=self.coach,
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_date=self.target_date,
            weekday=self.target_date.weekday(),
            start_hour=19,
            capacity=5,
            weeks_ahead=1,
            is_active=True,
        )
        replacement.members.add(replacement_member)

        original_id = self.reservation.pk
        consumed_at = Reservation.objects.get(pk=original_id).ticket_consumed_at
        synchronize_fixed_lesson_membership(replacement.pk)

        old_reservation = Reservation.objects.get(pk=original_id)
        self.assertEqual(old_reservation.fixed_lesson_id, replacement.pk)
        self.assertEqual(old_reservation.ticket_consumed_at, consumed_at)
        self.assertEqual(old_reservation.tickets_used, 1)
        self.assertFalse(old_reservation.is_fixed_entry)
        active = Reservation.objects.filter(
            availability=availability,
            status=Reservation.STATUS_ACTIVE,
        )
        self.assertEqual(active.count(), 2)

        response = self.client.get(
            reverse("club:lesson_calendar"),
            {"year": self.target_date.year, "month": self.target_date.month},
        )
        row = next(
            item for item in response.context["schedule_rows"]
            if item["fixed_lesson_id"] == str(replacement.pk)
        )
        self.assertEqual(row["member_count"], 2)

        self.client.force_login(self.coach)
        member_response = self.client.get(
            reverse("club:lesson_calendar_member_list"),
            {
                "availability_id": availability.pk,
                "fixed_lesson_id": replacement.pk,
                "lesson_date": self.target_date.isoformat(),
            },
        )
        self.assertEqual(member_response.status_code, 200)
        self.assertEqual(len(member_response.context["active_rows"]), 2)

    def test_availability_identity_does_not_merge_same_time_other_availability(self):
        start_at, end_at = self.fixed_lesson._build_datetimes_for_date(self.target_date)
        other_coach = User.objects.create_user(
            username="other-availability-coach", role=User.ROLE_COACH
        )
        other_court = Court.objects.create(name="other-availability-court")
        first = CoachAvailability.objects.create(
            coach=self.coach, court=self.court, lesson_type=Reservation.LESSON_GENERAL,
            start_at=start_at, end_at=end_at, capacity=5,
        )
        other = CoachAvailability.objects.create(
            coach=other_coach, court=other_court, lesson_type=Reservation.LESSON_GENERAL,
            start_at=start_at, end_at=end_at, capacity=5,
        )
        Reservation.objects.filter(pk=self.reservation.pk).update(availability=first)
        other_member = User.objects.create_user(username="other-availability-member", role=User.ROLE_MEMBER)
        Reservation.objects.bulk_create([Reservation(
            user=other_member, coach=other_coach, court=other_court, availability=other,
            fixed_lesson=self.fixed_lesson, lesson_type=Reservation.LESSON_GENERAL,
            start_at=start_at, end_at=end_at, status=Reservation.STATUS_ACTIVE,
        )])
        from club.lesson_participants import reservations_for_lesson
        self.assertEqual(
            reservations_for_lesson(
                fixed_lesson=self.fixed_lesson, availability=first,
                lesson_type=Reservation.LESSON_GENERAL, start_at=start_at, end_at=end_at,
            ).count(),
            1,
        )
