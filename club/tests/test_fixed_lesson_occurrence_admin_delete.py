from datetime import datetime, time, timedelta

from django.contrib.admin.sites import AdminSite
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from club.admin import CoachAvailabilityAdmin
from club.fixed_lesson_membership_service import synchronize_fixed_lesson_membership
from club.fixed_lesson_occurrence_service import delete_or_cancel_availability
from club.models import (
    CoachAvailability,
    Court,
    FixedLesson,
    FixedLessonCanceledOccurrence,
    LessonWaitlist,
    Reservation,
    User,
)


class FixedLessonOccurrenceAdminDeleteTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(
            username="occurrence-admin-coach", role=User.ROLE_COACH
        )
        self.member = User.objects.create_user(
            username="occurrence-admin-member",
            role=User.ROLE_MEMBER,
            member_level=User.LEVEL_BEGINNER,
            ticket_balance=4,
        )
        self.waiting_member = User.objects.create_user(
            username="occurrence-admin-waiting",
            role=User.ROLE_MEMBER,
            member_level=User.LEVEL_BEGINNER,
        )
        self.admin_user = User.objects.create_superuser(
            username="occurrence-admin", email="admin@example.com", password="pw"
        )
        self.court = Court.objects.create(name="開催回中止テストコート")
        first_date = timezone.localdate() + timedelta(days=1)
        self.fixed_lesson = FixedLesson.objects.create(
            title="開催回中止テスト",
            coach=self.coach,
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_date=first_date,
            weekday=first_date.weekday(),
            start_hour=19,
            weeks_ahead=3,
            is_active=True,
        )
        self.fixed_lesson.members.add(self.member)
        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)
        self.occurrence_dates = self.fixed_lesson.scheduled_occurrence_dates()
        self.target_date = self.occurrence_dates[1]
        self.target_reservation = Reservation.objects.get(
            fixed_lesson=self.fixed_lesson,
            user=self.member,
            start_at__date=self.target_date,
            status=Reservation.STATUS_ACTIVE,
        )
        self.target_availability = self.target_reservation.availability
        self.waitlist = LessonWaitlist.objects.create(
            user=self.waiting_member,
            coach=self.coach,
            court=self.court,
            availability=self.target_availability,
            fixed_lesson=self.fixed_lesson,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_at=self.target_reservation.start_at,
            end_at=self.target_reservation.end_at,
        )
        request = RequestFactory().post("/admin/club/coachavailability/")
        request.user = self.admin_user
        CoachAvailabilityAdmin(CoachAvailability, AdminSite()).delete_model(
            request, self.target_availability
        )

    def test_admin_delete_cancels_only_selected_fixed_occurrence(self):
        self.fixed_lesson.refresh_from_db()
        self.target_reservation.refresh_from_db()
        self.waitlist.refresh_from_db()

        self.assertTrue(FixedLesson.objects.filter(pk=self.fixed_lesson.pk).exists())
        self.assertTrue(
            FixedLessonCanceledOccurrence.objects.filter(
                fixed_lesson=self.fixed_lesson,
                occurrence_date=self.target_date,
            ).exists()
        )
        self.assertNotIn(self.target_date, self.fixed_lesson.scheduled_occurrence_dates())
        self.assertEqual(self.target_reservation.status, Reservation.STATUS_CANCELED)
        self.assertEqual(self.waitlist.status, LessonWaitlist.STATUS_CANCELED)
        self.assertFalse(CoachAvailability.objects.filter(pk=self.target_availability.pk).exists())
        other_active_dates = set(
            Reservation.objects.filter(
                fixed_lesson=self.fixed_lesson,
                status=Reservation.STATUS_ACTIVE,
            ).values_list("start_at__date", flat=True)
        )
        self.assertSetEqual(other_active_dates, {self.occurrence_dates[0], self.occurrence_dates[2]})

    def test_sync_does_not_recreate_canceled_occurrence_or_refund_twice(self):
        ticket_balance = self.member.ticket_balance
        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)
        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)

        self.assertFalse(
            CoachAvailability.objects.filter(
                coach=self.coach,
                start_at__date=self.target_date,
            ).exists()
        )
        self.assertFalse(
            Reservation.objects.filter(
                fixed_lesson=self.fixed_lesson,
                start_at__date=self.target_date,
                status__in=(Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING),
            ).exists()
        )
        self.member.refresh_from_db()
        self.assertEqual(self.member.ticket_balance, ticket_balance)

    def test_calendar_omits_only_canceled_occurrence(self):
        self.client.force_login(self.member)
        response = self.client.get(
            reverse("club:lesson_calendar"),
            {"year": self.target_date.year, "month": self.target_date.month},
        )
        self.assertEqual(response.status_code, 200)
        displayed_dates = {
            row["lesson_date"]
            for row in response.context["schedule_rows"]
            if row.get("fixed_lesson_id") == str(self.fixed_lesson.pk)
        }
        self.assertNotIn(self.target_date.isoformat(), displayed_dates)
        for occurrence_date in (self.occurrence_dates[0], self.occurrence_dates[2]):
            if occurrence_date.month == self.target_date.month:
                self.assertIn(occurrence_date.isoformat(), displayed_dates)


class RegularAvailabilityAdminDeleteTests(TestCase):
    def test_regular_availability_is_deleted_without_cancellation_record(self):
        coach = User.objects.create_user(username="regular-admin-coach", role=User.ROLE_COACH)
        admin_user = User.objects.create_superuser(
            username="regular-admin", email="regular@example.com", password="pw"
        )
        court = Court.objects.create(name="通常枠削除テストコート")
        target_date = timezone.localdate() + timedelta(days=2)
        start_at = timezone.make_aware(datetime.combine(target_date, time(10, 0)))
        availability = CoachAvailability.objects.create(
            coach=coach,
            court=court,
            lesson_type=CoachAvailability.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=start_at + timedelta(hours=2),
        )
        request = RequestFactory().post("/admin/club/coachavailability/")
        request.user = admin_user

        CoachAvailabilityAdmin(CoachAvailability, AdminSite()).delete_model(request, availability)

        self.assertFalse(CoachAvailability.objects.filter(pk=availability.pk).exists())
        self.assertFalse(FixedLessonCanceledOccurrence.objects.exists())


class LegacyFixedLessonOccurrenceDeleteTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(username="legacy-coach", role=User.ROLE_COACH)
        self.member = User.objects.create_user(
            username="legacy-member",
            role=User.ROLE_MEMBER,
            member_level=User.LEVEL_INTERMEDIATE,
        )
        self.admin_user = User.objects.create_superuser(
            username="legacy-admin", email="legacy@example.com", password="pw"
        )
        self.court = Court.objects.create(name="Legacy availability court")
        self.target_date = timezone.localdate() + timedelta(days=8)
        self.start_at = timezone.make_aware(datetime.combine(self.target_date, time(19)))
        self.fixed_lesson = self._fixed_lesson("中級・中上級", court=None)

    def _fixed_lesson(self, title, *, court=None):
        return FixedLesson.objects.create(
            title=title,
            coach=self.coach,
            court=court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=User.LEVEL_INTERMEDIATE,
            target_level_2=User.LEVEL_INTERMEDIATE_PLUS,
            start_date=self.target_date,
            weekday=self.target_date.weekday(),
            start_hour=19,
            weeks_ahead=3,
            is_active=True,
        )

    def _availability(self, **overrides):
        values = {
            "coach": self.coach,
            "court": self.court,
            "lesson_type": CoachAvailability.LESSON_GENERAL,
            "target_level": User.LEVEL_INTERMEDIATE,
            "target_level_2": User.LEVEL_INTERMEDIATE_PLUS,
            "start_at": self.start_at,
            "end_at": self.start_at + timedelta(hours=2),
            "note": "固定レッスン: 中級・中上級",
        }
        values.update(overrides)
        return CoachAvailability.objects.create(**values)

    def _reservation(self, availability, fixed_lesson, *, status):
        return Reservation.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court,
            availability=availability,
            fixed_lesson=fixed_lesson,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_INTERMEDIATE,
            start_at=availability.start_at,
            end_at=availability.end_at,
            status=status,
        )

    def _request(self):
        request = RequestFactory().post("/admin/club/coachavailability/")
        request.user = self.admin_user
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def test_canceled_reservation_link_overrides_historical_court_mismatch(self):
        availability = self._availability()
        reservation = self._reservation(
            availability, self.fixed_lesson, status=Reservation.STATUS_CANCELED
        )

        was_fixed = delete_or_cancel_availability(availability_id=availability.pk)

        self.assertTrue(was_fixed)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.STATUS_CANCELED)
        self.assertFalse(CoachAvailability.objects.filter(pk=availability.pk).exists())
        self.assertTrue(FixedLesson.objects.filter(pk=self.fixed_lesson.pk).exists())
        self.assertTrue(FixedLessonCanceledOccurrence.objects.filter(
            fixed_lesson=self.fixed_lesson, occurrence_date=self.target_date
        ).exists())

    def test_active_reservation_is_canceled_for_only_the_selected_date(self):
        availability = self._availability()
        reservation = self._reservation(
            availability, self.fixed_lesson, status=Reservation.STATUS_ACTIVE
        )

        delete_or_cancel_availability(availability_id=availability.pk)

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.STATUS_CANCELED)
        self.assertEqual(
            list(FixedLessonCanceledOccurrence.objects.values_list("occurrence_date", flat=True)),
            [self.target_date],
        )

    def test_waitlist_link_is_authoritative_without_reservation(self):
        availability = self._availability()
        waitlist = LessonWaitlist.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court,
            availability=availability,
            fixed_lesson=self.fixed_lesson,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_INTERMEDIATE,
            start_at=availability.start_at,
            end_at=availability.end_at,
        )

        delete_or_cancel_availability(availability_id=availability.pk)

        waitlist.refresh_from_db()
        self.assertEqual(waitlist.status, LessonWaitlist.STATUS_CANCELED)
        self.assertTrue(FixedLessonCanceledOccurrence.objects.filter(
            fixed_lesson=self.fixed_lesson, occurrence_date=self.target_date
        ).exists())

    def test_source_is_authoritative_without_participant_links(self):
        availability = self._availability(fixed_lesson_source=self.fixed_lesson, note="")

        self.assertTrue(delete_or_cancel_availability(availability_id=availability.pk))
        self.assertTrue(FixedLessonCanceledOccurrence.objects.filter(
            fixed_lesson=self.fixed_lesson, occurrence_date=self.target_date
        ).exists())

    def test_unique_legacy_note_is_resolved_but_ambiguous_note_is_rejected(self):
        unique = self._availability()
        self.assertTrue(delete_or_cancel_availability(availability_id=unique.pk))

        FixedLessonCanceledOccurrence.objects.all().delete()
        second = self._fixed_lesson("中級・中上級", court=None)
        ambiguous = self._availability()
        with self.assertRaisesMessage(ValidationError, "候補が複数"):
            delete_or_cancel_availability(availability_id=ambiguous.pk)
        self.assertTrue(CoachAvailability.objects.filter(pk=ambiguous.pk).exists())
        self.assertTrue(FixedLesson.objects.filter(pk=second.pk).exists())

    def test_admin_conflicting_explicit_links_reports_error_without_500(self):
        other = self._fixed_lesson("別固定", court=self.court)
        availability = self._availability()
        self._reservation(availability, self.fixed_lesson, status=Reservation.STATUS_CANCELED)
        LessonWaitlist.objects.create(
            user=User.objects.create_user(
                username="other-waiting", member_level=User.LEVEL_INTERMEDIATE
            ),
            coach=self.coach,
            court=self.court,
            availability=availability,
            fixed_lesson=other,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_INTERMEDIATE,
            start_at=availability.start_at,
            end_at=availability.end_at,
        )
        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse("admin:club_coachavailability_delete", args=[availability.pk]),
            {"post": "yes"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(CoachAvailability.objects.filter(pk=availability.pk).exists())
        self.assertContains(response, "異なる固定レッスン")

    def test_bulk_delete_rolls_back_all_rows_when_one_is_ambiguous(self):
        other_coach = User.objects.create_user(username="bulk-coach", role=User.ROLE_COACH)
        regular = self._availability(coach=other_coach, note="")
        ambiguous = self._availability()
        other = self._fixed_lesson("別固定", court=self.court)
        self._reservation(ambiguous, self.fixed_lesson, status=Reservation.STATUS_CANCELED)
        LessonWaitlist.objects.create(
            user=User.objects.create_user(
                username="bulk-waiting", member_level=User.LEVEL_INTERMEDIATE
            ),
            coach=self.coach,
            court=self.court,
            availability=ambiguous,
            fixed_lesson=other,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_INTERMEDIATE,
            start_at=ambiguous.start_at,
            end_at=ambiguous.end_at,
        )
        request = self._request()

        CoachAvailabilityAdmin(CoachAvailability, AdminSite()).delete_queryset(
            request, CoachAvailability.objects.filter(pk__in=[regular.pk, ambiguous.pk])
        )

        self.assertTrue(CoachAvailability.objects.filter(pk=regular.pk).exists())
        self.assertTrue(CoachAvailability.objects.filter(pk=ambiguous.pk).exists())
        self.assertFalse(FixedLessonCanceledOccurrence.objects.exists())
        self.assertIn("異なる固定レッスン", " ".join(str(item) for item in get_messages(request)))
