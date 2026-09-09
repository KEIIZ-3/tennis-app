from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace

from django.apps import apps
from django.contrib.admin.sites import AdminSite
from django.test import TestCase
from django.utils import timezone

from club.admin import CoachAvailabilityAdmin
from club.fixed_lesson_membership_service import (
    restore_fixed_lesson_coach_assignment,
)
from club.fixed_lesson_sync_facade import synchronize_fixed_lesson_membership
from club.models import CoachAvailability, Court, FixedLesson, Reservation, User


class FixedLessonCoachAssignmentOverrideTests(TestCase):
    def setUp(self):
        self.coach_a = User.objects.create_user(username="override-a", role=User.ROLE_COACH)
        self.coach_b = User.objects.create_user(username="override-b", role=User.ROLE_COACH)
        self.coach_c = User.objects.create_user(username="override-c", role=User.ROLE_COACH)
        self.court = Court.objects.create(name="override-court", available_court_count=3)
        self.member = User.objects.create_user(
            username="override-member", role=User.ROLE_MEMBER
        )
        target_date = timezone.localdate() + timedelta(days=1)
        self.fixed_lesson = FixedLesson.objects.create(
            title="override source",
            coach=self.coach_a,
            coach_2=self.coach_b,
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            start_date=target_date,
            weekday=target_date.weekday(),
            start_hour=10,
            coach_count=2,
            court_count=2,
            capacity=10,
            weeks_ahead=1,
        )

    def _availability(self):
        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)
        return CoachAvailability.objects.get(fixed_lesson_source=self.fixed_lesson)

    def _link_reservation(self, availability, fixed_lesson=None):
        return Reservation.objects.create(
            user=self.member,
            coach=availability.coach,
            court=availability.court,
            availability=availability,
            fixed_lesson=fixed_lesson or self.fixed_lesson,
            lesson_type=availability.lesson_type,
            target_level=availability.target_level,
            start_at=availability.start_at,
            end_at=availability.end_at,
            status=Reservation.STATUS_ACTIVE,
        )

    def test_new_occurrence_records_provenance_and_inherits_coaches(self):
        availability = self._availability()

        self.assertFalse(availability.coach_assignment_overridden)
        self.assertEqual(availability.coach_id, self.coach_a.pk)
        self.assertEqual(availability.coach_2_id, self.coach_b.pk)
        self.assertEqual(availability.coach_count, 2)

    def test_fixed_lesson_body_change_updates_only_inheriting_occurrence(self):
        availability = self._availability()
        self.fixed_lesson.coach_2 = self.coach_c
        self.fixed_lesson.save(update_fields=["coach_2"])

        availability.refresh_from_db()
        self.assertEqual(availability.coach_2_id, self.coach_c.pk)

    def test_override_survives_fixed_lesson_sync_and_preserves_capacity(self):
        availability = self._availability()
        availability.coach_2 = None
        availability.coach_assignment_overridden = True
        availability.save(update_fields=["coach_2", "coach_assignment_overridden"])

        self.fixed_lesson.coach_2 = self.coach_c
        self.fixed_lesson.save(update_fields=["coach_2"])
        availability.refresh_from_db()

        self.assertEqual(availability.coach_id, self.coach_a.pk)
        self.assertIsNone(availability.coach_2_id)
        self.assertEqual(availability.coach_count, 1)
        self.assertEqual(availability.capacity, 5)

    def test_admin_marks_actual_assignment_change_but_not_noop(self):
        availability = self._availability()
        model_admin = CoachAvailabilityAdmin(CoachAvailability, AdminSite())
        request = SimpleNamespace(user=None)

        model_admin.save_model(request, availability, form=None, change=True)
        availability.refresh_from_db()
        self.assertFalse(availability.coach_assignment_overridden)

        availability.coach_2 = None
        model_admin.save_model(request, availability, form=None, change=True)
        availability.refresh_from_db()
        self.assertTrue(availability.coach_assignment_overridden)
        self.assertIsNone(availability.coach_2_id)

    def test_explicit_restore_clears_override_and_restores_assignment(self):
        availability = self._availability()
        availability.coach_2 = None
        availability.coach_assignment_overridden = True
        availability.save(update_fields=["coach_2", "coach_assignment_overridden"])

        restore_fixed_lesson_coach_assignment(availability.pk)
        availability.refresh_from_db()

        self.assertFalse(availability.coach_assignment_overridden)
        self.assertEqual(availability.coach_2_id, self.coach_b.pk)
        self.assertEqual(availability.coach_count, 2)

    def test_one_time_availability_is_not_adopted_or_changed(self):
        start_at, end_at = self.fixed_lesson._build_datetimes_for_date(
            self.fixed_lesson.start_date
        )
        one_time = CoachAvailability.objects.create(
            coach=self.coach_c,
            court=self.court,
            start_at=start_at + timedelta(hours=3),
            end_at=end_at + timedelta(hours=3),
            capacity=5,
        )

        synchronize_fixed_lesson_membership(self.fixed_lesson.pk)
        one_time.refresh_from_db()

        self.assertIsNone(one_time.fixed_lesson_source_id)
        self.assertEqual(one_time.coach_id, self.coach_c.pk)

    def test_migration_protects_coherent_one_coach_legacy_override(self):
        availability = self._availability()
        self._link_reservation(availability)
        CoachAvailability.objects.filter(pk=availability.pk).update(
            coach_2=None,
            coach_count=1,
            capacity=5,
            fixed_lesson_source=None,
            coach_assignment_overridden=False,
        )

        migration = import_module(
            "club.migrations.0073_coach_availability_fixed_lesson_provenance"
        )
        migration.backfill_fixed_lesson_provenance(apps, None)
        availability.refresh_from_db()

        self.assertEqual(availability.fixed_lesson_source_id, self.fixed_lesson.pk)
        self.assertTrue(availability.coach_assignment_overridden)
        self.assertIsNone(availability.coach_2_id)
        self.assertEqual(availability.capacity, 5)

    def test_migration_repairs_legacy_missing_second_coach(self):
        availability = self._availability()
        self._link_reservation(availability)
        CoachAvailability.objects.filter(pk=availability.pk).update(
            coach_2=None,
            coach_count=2,
            fixed_lesson_source=None,
            coach_assignment_overridden=False,
        )

        migration = import_module(
            "club.migrations.0073_coach_availability_fixed_lesson_provenance"
        )
        migration.backfill_fixed_lesson_provenance(apps, None)
        availability.refresh_from_db()

        self.assertEqual(availability.fixed_lesson_source_id, self.fixed_lesson.pk)
        self.assertFalse(availability.coach_assignment_overridden)
        self.assertEqual(availability.coach_2_id, self.coach_b.pk)
        self.assertEqual(availability.coach_count, 2)
