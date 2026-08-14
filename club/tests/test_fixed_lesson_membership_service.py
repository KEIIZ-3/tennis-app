from datetime import timedelta
from unittest.mock import patch

from django.contrib import admin
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
from club.admin import FixedLessonAdmin, FixedLessonAdminForm


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
            form._save_m2m()

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


class FixedLessonAdminSaveTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            username="fixed-admin", password="test-password", email="admin@example.com"
        )
        self.coach = User.objects.create_user(
            username="admin-fixed-coach",
            role=User.ROLE_COACH,
            member_level=User.LEVEL_ADVANCED,
        )
        self.members = [
            User.objects.create_user(
                username=f"admin-fixed-member-{index}",
                role=User.ROLE_MEMBER,
                member_level=User.LEVEL_ADVANCED,
                is_profile_completed=True,
            )
            for index in range(2)
        ]
        self.court = Court.objects.create(
            name="Admin fixed lesson court", court_type=Court.COURT_OTHER
        )
        self.start_date = timezone.localdate() + timedelta(days=1)
        self.client.force_login(self.admin_user)

    def _post_data(self, members=(), **extra):
        data = {
            "title": "Admin fixed lesson",
            "is_active": "on",
            "lesson_type": FixedLesson.LESSON_GENERAL,
            "target_level": User.LEVEL_BEGINNER,
            "target_level_2": "",
            "start_date": self.start_date.isoformat(),
            "weekday": str(self.start_date.weekday()),
            "start_hour": "19",
            "weeks_ahead": "3",
            "coach": str(self.coach.pk),
            "coach_2": "",
            "coach_3": "",
            "court": str(self.court.pk),
            "coach_count": "1",
            "court_count": "1",
            "capacity": "6",
            "members": [str(member.pk) for member in members],
            "note": "",
            "_save": "Save",
        }
        data.update(extra)
        return data

    def _add(self, members=(), **extra):
        return self.client.post(
            reverse("admin:club_fixedlesson_add"),
            self._post_data(members, **extra),
        )

    def test_admin_add_saves_zero_members(self):
        response = self._add()
        self.assertEqual(response.status_code, 302)
        lesson = FixedLesson.objects.get(title="Admin fixed lesson")
        self.assertEqual(lesson.members.count(), 0)
        self.assertEqual(Reservation.objects.filter(fixed_lesson=lesson).count(), 0)

    def test_admin_add_saves_one_member_and_future_reservations(self):
        response = self._add([self.members[0]])
        self.assertEqual(response.status_code, 302)
        lesson = FixedLesson.objects.get(title="Admin fixed lesson")
        self.assertEqual(list(lesson.members.all()), [self.members[0]])
        self.assertEqual(
            Reservation.objects.filter(
                fixed_lesson=lesson,
                user=self.members[0],
                status=Reservation.STATUS_ACTIVE,
            ).count(),
            3,
        )

    def test_admin_add_saves_multiple_members_with_one_service_sync(self):
        from club import fixed_lesson_sync_facade

        original_sync = fixed_lesson_sync_facade.synchronize_fixed_lesson_membership
        with patch.object(
            fixed_lesson_sync_facade,
            "synchronize_fixed_lesson_membership",
            wraps=original_sync,
        ) as sync_mock:
            response = self._add(self.members)

        self.assertEqual(response.status_code, 302)
        lesson = FixedLesson.objects.get(title="Admin fixed lesson")
        self.assertEqual(set(lesson.members.all()), set(self.members))
        self.assertEqual(sync_mock.call_count, 1)
        self.assertEqual(
            Reservation.objects.filter(
                fixed_lesson=lesson, status=Reservation.STATUS_ACTIVE
            ).count(),
            6,
        )

    def test_admin_change_add_remove_clear_and_idempotent_resave(self):
        self._add([self.members[0]])
        lesson = FixedLesson.objects.get(title="Admin fixed lesson")
        change_url = reverse("admin:club_fixedlesson_change", args=[lesson.pk])

        response = self.client.post(change_url, self._post_data(self.members))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(set(lesson.members.all()), set(self.members))
        active = Reservation.objects.filter(
            fixed_lesson=lesson, status=Reservation.STATUS_ACTIVE
        )
        self.assertEqual(active.count(), 6)

        response = self.client.post(change_url, self._post_data([self.members[1]]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(lesson.members.all()), [self.members[1]])
        self.assertEqual(active.count(), 3)
        self.assertFalse(active.filter(user=self.members[0]).exists())

        response = self.client.post(change_url, self._post_data([self.members[1]]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(active.count(), 3)

        response = self.client.post(change_url, self._post_data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(lesson.members.count(), 0)
        self.assertEqual(active.count(), 0)

    def test_admin_save_buttons_use_same_membership_path(self):
        button_cases = (
            {"_save": "Save"},
            {"_save": "", "_continue": "Save and continue editing"},
            {"_save": "", "_addanother": "Save and add another"},
        )
        for index, button_data in enumerate(button_cases):
            with self.subTest(button_data=button_data):
                data = self._post_data([self.members[0]], **button_data)
                data["title"] = f"Admin fixed lesson {index}"
                response = self.client.post(reverse("admin:club_fixedlesson_add"), data)
                self.assertEqual(response.status_code, 302)
                lesson = FixedLesson.objects.get(title=data["title"])
                self.assertEqual(list(lesson.members.all()), [self.members[0]])

    def test_admin_add_rolls_back_lesson_and_members_when_sync_fails(self):
        with patch(
            "club.fixed_lesson_sync_facade.synchronize_fixed_lesson_membership",
            side_effect=RuntimeError("sync failed"),
        ):
            with self.assertRaisesMessage(RuntimeError, "sync failed"):
                self._add([self.members[0]])

        self.assertFalse(FixedLesson.objects.filter(title="Admin fixed lesson").exists())

    def test_ready_reentry_does_not_duplicate_membership_receiver(self):
        from django.apps import apps
        from club import signals

        config = apps.get_app_config("club")
        config.ready()
        config.ready()
        with patch.object(
            signals,
            "synchronize_fixed_lesson_membership",
            return_value=0,
        ) as sync_mock:
            lesson = FixedLesson.objects.create(
                title="Ready reentry lesson",
                coach=self.coach,
                court=self.court,
                lesson_type=FixedLesson.LESSON_GENERAL,
                target_level=User.LEVEL_BEGINNER,
                start_date=self.start_date,
                weekday=self.start_date.weekday(),
                start_hour=19,
                capacity=6,
                coach_count=1,
                court_count=1,
                weeks_ahead=3,
            )
            lesson.members.add(self.members[0])

        sync_mock.assert_called_once_with(lesson.pk)


class FixedLessonAdminActionServiceTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            username="fixed-action-admin",
            password="test-password",
            email="action-admin@example.com",
        )
        self.coach = User.objects.create_user(
            username="fixed-action-coach",
            role=User.ROLE_COACH,
            member_level=User.LEVEL_ADVANCED,
        )
        self.court = Court.objects.create(
            name="Fixed action court",
            court_type=Court.COURT_OTHER,
        )
        start_date = timezone.localdate() + timedelta(days=1)
        self.lesson = FixedLesson.objects.create(
            title="Fixed action lesson",
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
            weeks_ahead=1,
            is_active=False,
        )

    def test_activate_action_calls_service_once_without_direct_update(self):
        model_admin = FixedLessonAdmin(FixedLesson, admin.site)
        request = type("Request", (), {"user": self.admin_user})()
        queryset = FixedLesson.objects.filter(pk=self.lesson.pk)

        with patch(
            "club.fixed_lesson_sync_facade.set_fixed_lesson_activity",
            return_value={"changed": True, "synchronized_count": 0},
        ) as service_mock, patch.object(model_admin, "message_user"):
            model_admin.activate_selected_fixed_lessons(request, queryset)

        service_mock.assert_called_once_with(
            self.lesson.pk,
            is_active=True,
            created_by=self.admin_user,
        )
        self.lesson.refresh_from_db()
        self.assertFalse(self.lesson.is_active)

    def test_activity_change_rolls_back_when_synchronization_fails(self):
        from club.fixed_lesson_sync_facade import set_fixed_lesson_activity

        with patch(
            "club.fixed_lesson_sync_facade.synchronize_fixed_lesson_membership",
            side_effect=RuntimeError("sync failed"),
        ):
            with self.assertRaisesMessage(RuntimeError, "sync failed"):
                set_fixed_lesson_activity(
                    self.lesson.pk,
                    is_active=True,
                    created_by=self.admin_user,
                )

        self.lesson.refresh_from_db()
        self.assertFalse(self.lesson.is_active)
