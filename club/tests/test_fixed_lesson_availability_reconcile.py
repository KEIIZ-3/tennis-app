from datetime import timedelta
from importlib import import_module

from django.apps import apps
from django.test import TestCase
from django.utils import timezone

from club.fixed_lesson_occurrence_service import (
    reconcile_fixed_lesson_availability,
    reconcile_future_unclassified_availabilities,
)
from club.fixed_lesson_sync_facade import synchronize_fixed_lesson_membership
from club.models import CoachAvailability, Court, FixedLesson, Reservation, User
from club.reservation_service import create_reservation


class FixedLessonAvailabilityReconcileTests(TestCase):
    def setUp(self):
        self.coach_a = User.objects.create_user(username="reconcile-a", role=User.ROLE_COACH)
        self.coach_b = User.objects.create_user(username="reconcile-b", role=User.ROLE_COACH)
        self.coach_c = User.objects.create_user(username="reconcile-c", role=User.ROLE_COACH)
        self.member = User.objects.create_user(username="reconcile-member", role=User.ROLE_MEMBER)
        self.member_2 = User.objects.create_user(username="reconcile-member-2", role=User.ROLE_MEMBER)
        self.court = Court.objects.create(name="reconcile-court", available_court_count=3)
        self.target_date = timezone.localdate() + timedelta(days=7)
        self.fixed_lesson = self._fixed_lesson("reconcile-fixed", self.coach_a, self.coach_b)

    def _fixed_lesson(self, title, coach, coach_2):
        return FixedLesson.objects.create(
            title=title,
            coach=coach,
            coach_2=coach_2,
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            start_date=self.target_date,
            weekday=self.target_date.weekday(),
            start_hour=10,
            coach_count=2,
            court_count=2,
            capacity=10,
            weeks_ahead=1,
        )

    def _availability(self, *, coach_count, coach_2=None, source=None, overridden=False):
        start_at, end_at = self.fixed_lesson._build_datetimes_for_date(self.target_date)
        availability = CoachAvailability(
            coach=self.coach_a,
            coach_2=coach_2,
            court=self.court,
            lesson_type=self.fixed_lesson.lesson_type,
            start_at=start_at,
            end_at=end_at,
            coach_count=coach_count,
            court_count=2,
            capacity=5,
            fixed_lesson_source=source,
            coach_assignment_overridden=overridden,
        )
        CoachAvailability.objects.bulk_create([availability])
        return availability

    def _reservation(self, availability, fixed_lesson=None, user=None):
        return Reservation(
            user=user or self.member,
            coach=availability.coach,
            court=availability.court,
            availability=availability,
            fixed_lesson=fixed_lesson or self.fixed_lesson,
            is_fixed_entry=True,
            lesson_type=availability.lesson_type,
            start_at=availability.start_at,
            end_at=availability.end_at,
            status=Reservation.STATUS_ACTIVE,
        )

    def test_coherent_one_coach_relation_becomes_protected_override(self):
        availability = self._availability(coach_count=1)
        Reservation.objects.bulk_create([self._reservation(availability)])

        reconcile_fixed_lesson_availability(availability, self.fixed_lesson)
        availability.refresh_from_db()

        self.assertEqual(availability.fixed_lesson_source_id, self.fixed_lesson.pk)
        self.assertTrue(availability.coach_assignment_overridden)
        self.assertEqual(availability.coach_id, self.coach_a.pk)
        self.assertIsNone(availability.coach_2_id)
        self.assertEqual(availability.coach_count, 1)
        self.assertEqual(availability.capacity, 5)

    def test_missing_second_coach_is_repaired_as_inherited(self):
        availability = self._availability(coach_count=2)
        Reservation.objects.bulk_create([
            self._reservation(availability),
            self._reservation(availability, user=self.member_2),
        ])

        reconcile_fixed_lesson_availability(availability, self.fixed_lesson)
        availability.refresh_from_db()

        self.assertEqual(availability.fixed_lesson_source_id, self.fixed_lesson.pk)
        self.assertFalse(availability.coach_assignment_overridden)
        self.assertEqual(availability.coach_2_id, self.coach_b.pk)
        self.assertEqual(availability.coach_count, 2)
        self.assertEqual(availability.capacity, 5)

    def test_ambiguous_reservation_relation_is_unchanged(self):
        other = self._fixed_lesson("reconcile-other", self.coach_a, self.coach_c)
        availability = self._availability(coach_count=2)
        Reservation.objects.bulk_create([
            self._reservation(availability),
            self._reservation(availability, other, self.member_2),
        ])

        outcome = reconcile_fixed_lesson_availability(availability, self.fixed_lesson)
        availability.refresh_from_db()

        self.assertEqual(outcome["status"], "ambiguous_relation")
        self.assertIsNone(availability.fixed_lesson_source_id)
        self.assertIsNone(availability.coach_2_id)

    def test_different_existing_source_is_not_overwritten(self):
        other = self._fixed_lesson("reconcile-source", self.coach_a, self.coach_c)
        availability = self._availability(coach_count=1, source=other, overridden=True)

        outcome = reconcile_fixed_lesson_availability(availability, self.fixed_lesson)
        availability.refresh_from_db()

        self.assertEqual(outcome["status"], "source_conflict")
        self.assertEqual(availability.fixed_lesson_source_id, other.pk)

    def test_existing_override_is_idempotently_preserved(self):
        availability = self._availability(
            coach_count=1,
            source=self.fixed_lesson,
            overridden=True,
        )

        first = reconcile_fixed_lesson_availability(availability, self.fixed_lesson)
        second = reconcile_fixed_lesson_availability(availability, self.fixed_lesson)
        availability.refresh_from_db()

        self.assertEqual(first["changed_fields"], [])
        self.assertEqual(second["changed_fields"], [])
        self.assertIsNone(availability.coach_2_id)
        self.assertEqual(availability.capacity, 5)

    def test_inheriting_occurrence_tracks_second_coach_change(self):
        availability = self._availability(
            coach_count=2,
            coach_2=self.coach_b,
            source=self.fixed_lesson,
        )
        self.fixed_lesson.coach_2 = self.coach_c
        self.fixed_lesson.save(update_fields=["coach_2"])

        availability.refresh_from_db()
        self.assertEqual(availability.coach_2_id, self.coach_c.pk)

    def test_generated_occurrence_has_provenance_immediately(self):
        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)

        availability = CoachAvailability.objects.get(fixed_lesson_source=self.fixed_lesson)
        self.assertEqual(availability.coach_2_id, self.coach_b.pk)
        self.assertFalse(availability.coach_assignment_overridden)

    def test_canonical_reservation_creation_reconciles_unclassified_occurrence(self):
        availability = self._availability(coach_count=2)

        create_reservation(**{
            field: getattr(self._reservation(availability), field)
            for field in (
                "user", "coach", "court", "availability", "fixed_lesson",
                "is_fixed_entry", "lesson_type", "start_at", "end_at", "status",
            )
        })
        availability.refresh_from_db()

        self.assertEqual(availability.fixed_lesson_source_id, self.fixed_lesson.pk)
        self.assertEqual(availability.coach_2_id, self.coach_b.pk)
        self.assertFalse(availability.coach_assignment_overridden)

    def test_future_audit_repairs_unique_and_reports_ambiguous(self):
        repairable = self._availability(coach_count=2)
        ambiguous = self._availability(coach_count=2)
        other = self._fixed_lesson("reconcile-audit-other", self.coach_a, self.coach_c)
        Reservation.objects.bulk_create([
            self._reservation(repairable),
            self._reservation(ambiguous, user=self.member_2),
            self._reservation(ambiguous, other),
        ])

        result = reconcile_future_unclassified_availabilities()

        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["reconciled"], 1)
        self.assertEqual(result["ambiguous"], 1)
        self.assertEqual(result["remaining_unclassified"], 1)
        self.assertEqual(result["remaining_unique"], 0)

    def test_deploy_migration_repairs_legacy_missing_second_coach(self):
        availability = self._availability(coach_count=2)
        Reservation.objects.bulk_create([
            self._reservation(availability),
            self._reservation(availability, user=self.member_2),
        ])
        migration = import_module(
            "club.migrations.0074_reconcile_fixed_lesson_availability_provenance"
        )

        migration.reconcile_future_provenance(apps, None)
        availability.refresh_from_db()

        self.assertEqual(availability.fixed_lesson_source_id, self.fixed_lesson.pk)
        self.assertEqual(availability.coach_2_id, self.coach_b.pk)
        self.assertFalse(availability.coach_assignment_overridden)
