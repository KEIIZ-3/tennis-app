from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.models import (
    CoachAvailability,
    Court,
    FixedLesson,
    Reservation,
    ReservationParticipant,
    User,
)
from club.fixed_lesson_sync_facade import replace_fixed_lesson_members
from club.admin import FixedLessonAdminForm


class FixedLessonMembershipServiceTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(
            username="fixed-coach",
            password="test-password",
            role=User.ROLE_COACH,
            full_name="固定担当コーチ",
            member_level=User.LEVEL_ADVANCED,
        )
        self.member = User.objects.create_user(
            username="fixed-member",
            password="test-password",
            role=User.ROLE_MEMBER,
            full_name="固定参加会員",
            member_level=User.LEVEL_ADVANCED,
            ticket_balance=0,
            is_profile_completed=True,
            email="fixed@example.com",
            phone_number="09000000000",
        )
        self.court = Court.objects.create(
            name="固定レッスンテストコート",
            court_type=Court.COURT_OTHER,
        )

        start_date = timezone.localdate() + timedelta(days=1)
        self.fixed_lesson = FixedLesson.objects.create(
            title="固定メンバー同期テスト",
            coach=self.coach,
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_date=start_date,
            weekday=start_date.weekday(),
            start_hour=19,
            capacity=6,
            coach_count=1,
            court_count=1,
            weeks_ahead=3,
            is_active=True,
        )

    def test_zero_ticket_member_gets_all_future_fixed_reservations(self):
        self.fixed_lesson.members.add(self.member)

        reservations = Reservation.objects.filter(
            user=self.member,
            fixed_lesson=self.fixed_lesson,
            is_fixed_entry=True,
            status=Reservation.STATUS_ACTIVE,
        ).order_by("start_at")

        self.assertEqual(reservations.count(), 3)
        self.assertTrue(all(item.ticket_consumed_at is None for item in reservations))
        self.member.refresh_from_db()
        self.assertEqual(self.member.ticket_balance, 0)

    def test_fixed_reservations_have_self_participant_snapshots(self):
        self.fixed_lesson.members.add(self.member)

        reservations = Reservation.objects.filter(
            user=self.member,
            fixed_lesson=self.fixed_lesson,
            is_fixed_entry=True,
            status=Reservation.STATUS_ACTIVE,
        )
        snapshots = ReservationParticipant.objects.filter(
            reservation__in=reservations,
            participant_type="self",
            parent=self.member,
        )

        self.assertEqual(snapshots.count(), reservations.count())
        self.assertTrue(
            all(
                snapshot.participant_name == self.member.display_name()
                for snapshot in snapshots
            )
        )

    def test_removing_member_cancels_future_fixed_reservations(self):
        self.fixed_lesson.members.add(self.member)
        self.fixed_lesson.members.remove(self.member)

        self.assertFalse(
            Reservation.objects.filter(
                user=self.member,
                fixed_lesson=self.fixed_lesson,
                status=Reservation.STATUS_ACTIVE,
            ).exists()
        )
        self.assertEqual(
            Reservation.objects.filter(
                user=self.member,
                fixed_lesson=self.fixed_lesson,
                status=Reservation.STATUS_CANCELED,
                cancellation_reason="固定レッスンメンバー解除",
            ).count(),
            3,
        )

    def test_member_addition_is_idempotent(self):
        self.fixed_lesson.members.add(self.member)
        self.fixed_lesson.members.add(self.member)

        self.assertEqual(
            Reservation.objects.filter(
                user=self.member,
                fixed_lesson=self.fixed_lesson,
                is_fixed_entry=True,
                status=Reservation.STATUS_ACTIVE,
            ).count(),
            3,
        )

    def test_reverse_clear_cancels_fixed_reservations(self):
        self.fixed_lesson.members.add(self.member)

        self.member.fixed_lessons.clear()

        self.assertFalse(
            Reservation.objects.filter(
                user=self.member,
                fixed_lesson=self.fixed_lesson,
                status=Reservation.STATUS_ACTIVE,
            ).exists()
        )

    def test_deactivation_cancels_future_fixed_reservations(self):
        self.fixed_lesson.members.add(self.member)
        self.fixed_lesson.is_active = False
        self.fixed_lesson.save(update_fields=["is_active"])

        changed_count = self.fixed_lesson.sync_future_reservations()

        self.assertEqual(changed_count, 3)
        self.assertFalse(
            Reservation.objects.filter(
                user=self.member,
                fixed_lesson=self.fixed_lesson,
                status=Reservation.STATUS_ACTIVE,
            ).exists()
        )

    def test_reactivation_restores_future_fixed_reservations(self):
        self.fixed_lesson.members.add(self.member)
        self.fixed_lesson.is_active = False
        self.fixed_lesson.save(update_fields=["is_active"])
        self.fixed_lesson.sync_future_reservations()

        self.fixed_lesson.is_active = True
        self.fixed_lesson.save(update_fields=["is_active"])
        changed_count = self.fixed_lesson.sync_future_reservations()

        self.assertEqual(changed_count, 3)
        self.assertEqual(
            Reservation.objects.filter(
                user=self.member,
                fixed_lesson=self.fixed_lesson,
                is_fixed_entry=True,
                status=Reservation.STATUS_ACTIVE,
            ).count(),
            3,
        )

    def test_changing_fixed_member_cancels_old_and_creates_new_reservations(self):
        replacement = User.objects.create_user(
            username="fixed-replacement-member",
            password="test-password",
            role=User.ROLE_MEMBER,
            full_name="固定交代会員",
            member_level=User.LEVEL_ADVANCED,
            is_profile_completed=True,
        )
        self.fixed_lesson.members.add(self.member)

        self.fixed_lesson.members.set([replacement])

        self.assertEqual(
            Reservation.objects.filter(
                user=self.member,
                fixed_lesson=self.fixed_lesson,
                status=Reservation.STATUS_CANCELED,
            ).count(),
            3,
        )
        self.assertEqual(
            Reservation.objects.filter(
                user=replacement,
                fixed_lesson=self.fixed_lesson,
                is_fixed_entry=True,
                status=Reservation.STATUS_ACTIVE,
            ).count(),
            3,
        )

    def test_canonical_service_replaces_members_with_one_synchronization(self):
        replacement = User.objects.create_user(
            username="canonical-replacement",
            role=User.ROLE_MEMBER,
            member_level=User.LEVEL_ADVANCED,
        )
        self.fixed_lesson.members.add(self.member)

        from club import fixed_lesson_sync_facade

        original_sync = fixed_lesson_sync_facade.synchronize_fixed_lesson_membership
        with patch.object(
            fixed_lesson_sync_facade,
            "synchronize_fixed_lesson_membership",
            wraps=original_sync,
        ) as sync_mock:
            result = replace_fixed_lesson_members(
                self.fixed_lesson,
                [replacement, replacement],
            )

        self.assertEqual(sync_mock.call_count, 1)
        self.assertEqual(result["added_ids"], {replacement.pk})
        self.assertEqual(result["removed_ids"], {self.member.pk})
        self.assertEqual(
            set(self.fixed_lesson.members.values_list("pk", flat=True)),
            {replacement.pk},
        )

    def test_canonical_service_no_change_still_repairs_once(self):
        self.fixed_lesson.members.add(self.member)
        from club import fixed_lesson_sync_facade

        original_sync = fixed_lesson_sync_facade.synchronize_fixed_lesson_membership
        with patch.object(
            fixed_lesson_sync_facade,
            "synchronize_fixed_lesson_membership",
            wraps=original_sync,
        ) as sync_mock:
            result = replace_fixed_lesson_members(self.fixed_lesson, [self.member])

        self.assertEqual(sync_mock.call_count, 1)
        self.assertEqual(result["added_ids"], set())
        self.assertEqual(result["removed_ids"], set())

    def test_canonical_service_rolls_back_members_when_sync_fails(self):
        replacement = User.objects.create_user(
            username="rollback-replacement",
            role=User.ROLE_MEMBER,
            member_level=User.LEVEL_ADVANCED,
        )
        self.fixed_lesson.members.add(self.member)

        with patch(
            "club.fixed_lesson_sync_facade.synchronize_fixed_lesson_membership",
            side_effect=RuntimeError("sync failed"),
        ):
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    replace_fixed_lesson_members(self.fixed_lesson, [replacement])

        self.assertEqual(
            set(self.fixed_lesson.members.values_list("pk", flat=True)),
            {self.member.pk},
        )

    def test_canonical_service_rejects_non_participant_role(self):
        with self.assertRaises(ValidationError):
            replace_fixed_lesson_members(self.fixed_lesson, [self.coach])

    def test_admin_form_routes_membership_through_canonical_service_once(self):
        form = object.__new__(FixedLessonAdminForm)
        form.instance = self.fixed_lesson
        form.cleaned_data = {"members": [self.member]}
        form.membership_created_by = self.coach

        with patch(
            "club.fixed_lesson_sync_facade.replace_fixed_lesson_members",
            return_value={
                "added_ids": {self.member.pk},
                "removed_ids": set(),
                "changed_count": 3,
            },
        ) as service_mock:
            form.save_m2m()

        service_mock.assert_called_once_with(
            self.fixed_lesson,
            [self.member],
            created_by=self.coach,
        )
        self.assertEqual(form.membership_sync_result["changed_count"], 3)

    def test_old_different_court_availability_is_reused_as_canonical_slot(self):
        old_court = Court.objects.create(
            name="旧コート",
            court_type=Court.COURT_OTHER,
        )
        target_date = self.fixed_lesson.scheduled_occurrence_dates()[0]
        start_at, end_at = self.fixed_lesson._build_datetimes_for_date(target_date)
        old_availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=old_court,
            lesson_type=self.fixed_lesson.lesson_type,
            target_level=self.fixed_lesson.target_level,
            start_at=start_at,
            end_at=end_at,
            capacity=6,
            coach_count=1,
            court_count=1,
            status=CoachAvailability.STATUS_OPEN,
            note="固定レッスン: 旧設定",
        )

        self.fixed_lesson.members.add(self.member)

        old_availability.refresh_from_db()
        self.assertEqual(old_availability.court_id, self.court.pk)
        self.assertEqual(
            CoachAvailability.objects.filter(
                coach=self.coach,
                lesson_type=self.fixed_lesson.lesson_type,
                start_at=start_at,
                end_at=end_at,
            ).count(),
            1,
        )
        reservation = Reservation.objects.get(
            user=self.member,
            fixed_lesson=self.fixed_lesson,
            start_at=start_at,
            status=Reservation.STATUS_ACTIVE,
        )
        self.assertEqual(reservation.availability_id, old_availability.pk)
        self.assertEqual(reservation.court_id, self.court.pk)

    def test_existing_normal_duplicate_is_canceled_and_fixed_reservation_remains(self):
        target_date = self.fixed_lesson.scheduled_occurrence_dates()[0]
        start_at, end_at = self.fixed_lesson._build_datetimes_for_date(target_date)
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=self.fixed_lesson.lesson_type,
            target_level=self.fixed_lesson.target_level,
            start_at=start_at,
            end_at=end_at,
            capacity=6,
            coach_count=1,
            court_count=1,
            status=CoachAvailability.STATUS_OPEN,
        )
        normal_reservation = Reservation.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court,
            availability=availability,
            lesson_type=self.fixed_lesson.lesson_type,
            target_level=self.fixed_lesson.target_level,
            start_at=start_at,
            end_at=end_at,
            status=Reservation.STATUS_ACTIVE,
        )

        self.fixed_lesson.members.add(self.member)

        normal_reservation.refresh_from_db()
        self.assertEqual(normal_reservation.status, Reservation.STATUS_CANCELED)
        self.assertEqual(
            normal_reservation.cancellation_reason,
            "固定メンバー予約との重複整理",
        )
        active = Reservation.objects.filter(
            user=self.member,
            lesson_type=self.fixed_lesson.lesson_type,
            start_at=start_at,
            end_at=end_at,
            status__in=[Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING],
        )
        self.assertEqual(active.count(), 1)
        self.assertTrue(active.get().is_fixed_entry)
        self.assertEqual(active.get().fixed_lesson_id, self.fixed_lesson.pk)

    def test_reservation_list_synchronizes_and_displays_fixed_reservation(self):
        self.fixed_lesson.members.add(self.member)
        Reservation.objects.filter(
            user=self.member,
            fixed_lesson=self.fixed_lesson,
        ).delete()
        self.fixed_lesson.sync_future_reservations()

        self.client.force_login(self.member)
        response = self.client.get(reverse("club:reservation_list"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            Reservation.objects.filter(
                user=self.member,
                fixed_lesson=self.fixed_lesson,
                is_fixed_entry=True,
                status=Reservation.STATUS_ACTIVE,
            ).exists()
        )
