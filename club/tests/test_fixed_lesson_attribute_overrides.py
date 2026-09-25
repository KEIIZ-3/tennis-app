from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace

from django.apps import apps
from django.contrib.admin.sites import AdminSite
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from club.admin import CoachAvailabilityAdmin
from club.fixed_lesson_sync_facade import synchronize_fixed_lesson_membership
from club.lesson_member_list import _capacity_for_slot
from club.models import CoachAvailability, Court, FixedLesson, Reservation, User


class FixedLessonAttributeOverrideTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(username="attribute-coach", role=User.ROLE_COACH)
        self.member = User.objects.create_user(username="attribute-member", role=User.ROLE_MEMBER)
        self.court_a = Court.objects.create(name="attribute-a", available_court_count=3)
        self.court_b = Court.objects.create(name="attribute-b", available_court_count=3)
        target_date = timezone.localdate() + timedelta(days=2)
        self.fixed = FixedLesson.objects.create(
            title="attribute source",
            coach=self.coach,
            court=self.court_a,
            lesson_type=FixedLesson.LESSON_GROUP,
            target_level=User.LEVEL_BEGINNER,
            start_date=target_date,
            weekday=target_date.weekday(),
            start_hour=10,
            capacity=10,
            weeks_ahead=1,
        )
        synchronize_fixed_lesson_membership(self.fixed.pk)
        self.availability = CoachAvailability.objects.get(fixed_lesson_source=self.fixed)

    def _sync_after_update(self, **updates):
        FixedLesson.objects.filter(pk=self.fixed.pk).update(**updates)
        synchronize_fixed_lesson_membership(self.fixed.pk)
        self.availability.refresh_from_db()

    def test_inheriting_groups_follow_fixed_lesson(self):
        self._sync_after_update(
            capacity=12,
            court=self.court_b,
            target_level=User.LEVEL_INTERMEDIATE,
            target_level_2=User.LEVEL_ADVANCED,
            title="changed title",
        )

        self.assertEqual(self.availability.capacity, 12)
        self.assertEqual(self.availability.court_id, self.court_b.pk)
        self.assertEqual(self.availability.target_level, User.LEVEL_INTERMEDIATE)
        self.assertEqual(self.availability.target_level_2, User.LEVEL_ADVANCED)
        self.assertEqual(self.availability.note, "固定レッスン: changed title")

    def test_each_overridden_group_is_preserved_independently(self):
        CoachAvailability.objects.filter(pk=self.availability.pk).update(
            capacity=5,
            capacity_overridden=True,
            court=self.court_b,
            court_assignment_overridden=True,
            target_level=User.LEVEL_ADVANCED,
            level_overridden=True,
            note="one occurrence",
            note_overridden=True,
        )
        self._sync_after_update(
            capacity=12,
            target_level=User.LEVEL_INTERMEDIATE,
            title="changed title",
        )

        self.assertEqual(self.availability.capacity, 5)
        self.assertEqual(self.availability.court_id, self.court_b.pk)
        self.assertEqual(self.availability.target_level, User.LEVEL_ADVANCED)
        self.assertEqual(self.availability.note, "one occurrence")

    def test_capacity_below_current_participants_is_rejected_atomically(self):
        for index in range(2):
            Reservation.objects.create(
                user=User.objects.create_user(username=f"participant-{index}", role=User.ROLE_MEMBER),
                coach=self.coach,
                court=self.court_a,
                availability=self.availability,
                fixed_lesson=self.fixed,
                lesson_type=self.fixed.lesson_type,
                target_level=self.fixed.target_level,
                start_at=self.availability.start_at,
                end_at=self.availability.end_at,
                status=Reservation.STATUS_ACTIVE,
            )
        FixedLesson.objects.filter(pk=self.fixed.pk).update(capacity=1)

        with self.assertRaises(ValidationError):
            synchronize_fixed_lesson_membership(self.fixed.pk)

        self.availability.refresh_from_db()
        self.assertEqual(self.availability.capacity, 10)

    def test_lesson_type_change_with_reservation_is_rejected(self):
        Reservation.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court_a,
            availability=self.availability,
            fixed_lesson=self.fixed,
            lesson_type=self.fixed.lesson_type,
            target_level=self.fixed.target_level,
            start_at=self.availability.start_at,
            end_at=self.availability.end_at,
        )
        FixedLesson.objects.filter(pk=self.fixed.pk).update(lesson_type=FixedLesson.LESSON_PRIVATE)

        with self.assertRaises(ValidationError):
            synchronize_fixed_lesson_membership(self.fixed.pk)

        self.availability.refresh_from_db()
        self.assertEqual(self.availability.lesson_type, FixedLesson.LESSON_GROUP)

    def test_lesson_type_override_preserves_existing_occurrence_and_reservation(self):
        reservation = Reservation.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court_a,
            availability=self.availability,
            fixed_lesson=self.fixed,
            lesson_type=self.fixed.lesson_type,
            target_level=self.fixed.target_level,
            start_at=self.availability.start_at,
            end_at=self.availability.end_at,
        )
        CoachAvailability.objects.filter(pk=self.availability.pk).update(
            lesson_type_overridden=True
        )

        self._sync_after_update(lesson_type=FixedLesson.LESSON_PRIVATE)

        reservation.refresh_from_db()
        self.assertEqual(self.availability.lesson_type, FixedLesson.LESSON_GROUP)
        self.assertEqual(reservation.lesson_type, FixedLesson.LESSON_GROUP)

    def test_lesson_type_without_participants_is_synchronized(self):
        self._sync_after_update(lesson_type=FixedLesson.LESSON_PRIVATE)
        self.assertEqual(self.availability.lesson_type, FixedLesson.LESSON_PRIVATE)

    def test_admin_marks_only_changed_group_and_noop_marks_none(self):
        model_admin = CoachAvailabilityAdmin(CoachAvailability, AdminSite())
        request = SimpleNamespace(user=None)
        model_admin.save_model(request, self.availability, form=None, change=True)
        self.availability.refresh_from_db()
        self.assertFalse(self.availability.capacity_overridden)
        self.assertFalse(self.availability.court_assignment_overridden)

        self.availability.capacity = 5
        model_admin.save_model(request, self.availability, form=None, change=True)
        self.availability.refresh_from_db()
        self.assertTrue(self.availability.capacity_overridden)
        self.assertFalse(self.availability.court_assignment_overridden)
        self.assertFalse(self.availability.level_overridden)
        self.assertFalse(self.availability.lesson_type_overridden)
        self.assertFalse(self.availability.note_overridden)

    def test_one_time_availability_is_not_changed(self):
        one_time = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court_b,
            lesson_type=CoachAvailability.LESSON_GROUP,
            start_at=self.availability.start_at + timedelta(hours=2),
            end_at=self.availability.end_at + timedelta(hours=2),
            capacity=4,
        )
        self._sync_after_update(capacity=12)
        one_time.refresh_from_db()
        self.assertEqual(one_time.capacity, 4)
        self.assertIsNone(one_time.fixed_lesson_source_id)

    def test_migration_protects_existing_differences_by_group(self):
        CoachAvailability.objects.filter(pk=self.availability.pk).update(
            capacity=5,
            note="existing individual note",
            capacity_overridden=False,
            note_overridden=False,
        )
        migration = import_module(
            "club.migrations.0075_coach_availability_attribute_overrides"
        )

        migration.preserve_existing_occurrence_differences(apps, None)

        self.availability.refresh_from_db()
        self.assertTrue(self.availability.capacity_overridden)
        self.assertTrue(self.availability.note_overridden)
        self.assertFalse(self.availability.court_assignment_overridden)


class GeneralLessonCapacityOverrideTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(
            username="general-capacity-coach", role=User.ROLE_COACH
        )
        self.coach_2 = User.objects.create_user(
            username="general-capacity-coach-2", role=User.ROLE_COACH
        )
        self.court = Court.objects.create(
            name="general-capacity-court", available_court_count=3
        )
        self.other_court = Court.objects.create(
            name="general-capacity-other-court", available_court_count=3
        )
        self.target_date = timezone.localdate() + timedelta(days=9)
        self.fixed = FixedLesson.objects.create(
            title="general capacity source",
            coach=self.coach,
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_date=self.target_date,
            weekday=self.target_date.weekday(),
            start_hour=10,
            capacity=5,
            coach_count=1,
            court_count=1,
            weeks_ahead=1,
        )
        synchronize_fixed_lesson_membership(self.fixed.pk)
        self.availability = CoachAvailability.objects.get(
            fixed_lesson_source=self.fixed
        )

    def _override_capacity(self, capacity=4):
        self.availability.capacity = capacity
        self.availability.capacity_overridden = True
        self.availability.save(update_fields=["capacity", "capacity_overridden"])
        self.availability.refresh_from_db()

    def _reservation(self, index):
        return Reservation.objects.create(
            user=User.objects.create_user(
                username=f"general-capacity-member-{index}", role=User.ROLE_MEMBER
            ),
            coach=self.availability.coach,
            court=self.availability.court,
            availability=self.availability,
            fixed_lesson=self.fixed,
            lesson_type=self.availability.lesson_type,
            target_level=self.availability.target_level,
            start_at=self.availability.start_at,
            end_at=self.availability.end_at,
            status=Reservation.STATUS_ACTIVE,
        )

    def test_non_overridden_general_capacity_remains_automatic(self):
        self.availability.capacity = 99
        self.availability.save()
        self.availability.refresh_from_db()
        self.assertEqual(self.availability.capacity, 5)
        self.assertEqual(self.availability.effective_capacity(), 5)

    def test_overridden_capacity_survives_save_note_and_level_changes(self):
        self._override_capacity()

        self.availability.save()
        self.availability.note = "occurrence note"
        self.availability.save(update_fields=["note"])
        self.availability.target_level = User.LEVEL_INTERMEDIATE
        self.availability.save(update_fields=["target_level"])
        self.availability.refresh_from_db()

        self.assertEqual(self.availability.capacity, 4)
        self.assertEqual(self.availability.effective_capacity(), 4)

    def test_sync_updates_inherited_capacity_and_preserves_override(self):
        self.fixed.coach_2 = self.coach_2
        self.fixed.coach_count = 2
        self.fixed.capacity = 10
        self.fixed.save()
        synchronize_fixed_lesson_membership(self.fixed.pk)
        self.availability.refresh_from_db()
        self.assertEqual(self.availability.capacity, 10)

        self._override_capacity(4)
        self.fixed.capacity = 5
        self.fixed.save(update_fields=["capacity"])
        synchronize_fixed_lesson_membership(self.fixed.pk)
        self.availability.refresh_from_db()
        self.assertEqual(self.availability.capacity, 4)

    def test_capacity_override_is_independent_from_coach_and_court_overrides(self):
        self._override_capacity()
        self.availability.coach_2 = self.coach_2
        self.availability.coach_assignment_overridden = True
        self.availability.court = self.other_court
        self.availability.court_assignment_overridden = True
        self.availability.save()

        self.assertEqual(self.availability.capacity, 4)
        self.assertEqual(self.availability.coach_count, 2)
        self.assertEqual(self.availability.court_count, 2)
        self.assertTrue(self.availability.coach_assignment_overridden)
        self.assertTrue(self.availability.court_assignment_overridden)

    def test_capacity_cannot_be_lowered_below_active_participants(self):
        self._override_capacity()
        for index in range(4):
            self._reservation(index)

        self.availability.capacity = 3
        with self.assertRaises(ValidationError):
            self.availability.save(update_fields=["capacity"])

        self.availability.refresh_from_db()
        self.assertEqual(self.availability.capacity, 4)

    def test_one_time_general_lesson_keeps_automatic_capacity(self):
        start_at = self.availability.start_at + timedelta(hours=3)
        one_time = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=CoachAvailability.LESSON_GENERAL,
            start_at=start_at,
            end_at=start_at + timedelta(hours=2),
            capacity=99,
        )
        self.assertIsNone(one_time.fixed_lesson_source_id)
        self.assertFalse(one_time.capacity_overridden)
        self.assertEqual(one_time.capacity, 5)

    def test_full_checks_and_member_counts_use_overridden_capacity(self):
        self._override_capacity()
        for index in range(4):
            self._reservation(index)

        self.assertEqual(
            _capacity_for_slot(
                availability=self.availability,
                fixed_lesson=self.fixed,
            ),
            4,
        )
        self.assertEqual(self.availability.reservations.count(), 4)
        self.assertEqual(4 - self.availability.reservations.count(), 0)

        fifth = Reservation(
            user=User.objects.create_user(
                username="general-capacity-member-fifth", role=User.ROLE_MEMBER
            ),
            coach=self.availability.coach,
            court=self.availability.court,
            availability=self.availability,
            fixed_lesson=self.fixed,
            lesson_type=self.availability.lesson_type,
            target_level=self.availability.target_level,
            start_at=self.availability.start_at,
            end_at=self.availability.end_at,
            status=Reservation.STATUS_ACTIVE,
        )
        with self.assertRaises(ValidationError):
            fifth.save()
