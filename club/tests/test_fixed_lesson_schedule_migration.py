from copy import copy
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.fixed_lesson_schedule_service import update_fixed_lesson_schedule
from club.fixed_lesson_sync_facade import synchronize_fixed_lesson_membership
from club.models import CoachAvailability, Court, FixedLesson, LessonWaitlist, Reservation


User = get_user_model()


class FixedLessonScheduleMigrationTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(username="schedule-coach", role=User.ROLE_COACH)
        self.member = User.objects.create_user(
            username="schedule-member", role=User.ROLE_MEMBER,
            member_level=User.LEVEL_BEGINNER, is_profile_completed=True,
        )
        self.court = Court.objects.create(name="Schedule court", court_type=Court.COURT_OTHER)
        start_date = timezone.localdate() + timedelta(days=7)
        self.fixed = FixedLesson.objects.create(
            title="Schedule migration", coach=self.coach, court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL, target_level=User.LEVEL_BEGINNER,
            start_date=start_date, weekday=start_date.weekday(), start_hour=19,
            capacity=4, coach_count=1, court_count=1, weeks_ahead=4,
        )
        synchronize_fixed_lesson_membership(self.fixed.pk)

    def _proposed(self, **changes):
        proposed = copy(self.fixed)
        for name, value in changes.items():
            setattr(proposed, name, value)
        return proposed

    def _occurrences(self):
        return list(CoachAvailability.objects.filter(
            fixed_lesson_source=self.fixed,
        ).order_by("start_at"))

    def test_weekday_change_without_bookings_replaces_unused_occurrences(self):
        old_ids = {item.pk for item in self._occurrences()}
        proposed = self._proposed(weekday=(self.fixed.weekday + 1) % 7)

        update_fixed_lesson_schedule(proposed)

        self.fixed.refresh_from_db()
        current = self._occurrences()
        self.assertEqual(self.fixed.weekday, proposed.weekday)
        self.assertEqual(len(current), 4)
        self.assertTrue(old_ids.isdisjoint({item.pk for item in current}))

    def test_active_reservation_rejects_change_and_rolls_back(self):
        self.fixed.members.add(self.member)
        availability_ids = set(self._occurrences())
        proposed = self._proposed(weekday=(self.fixed.weekday + 1) % 7)

        with self.assertRaises(ValidationError):
            update_fixed_lesson_schedule(proposed)

        self.fixed.refresh_from_db()
        self.assertNotEqual(self.fixed.weekday, proposed.weekday)
        self.assertEqual(set(self._occurrences()), availability_ids)
        self.assertEqual(Reservation.objects.filter(
            fixed_lesson=self.fixed, status=Reservation.STATUS_ACTIVE,
        ).count(), 4)

    def test_waitlist_rejects_change(self):
        availability = self._occurrences()[0]
        LessonWaitlist.objects.create(
            user=self.member, coach=self.coach, court=self.court,
            availability=availability, fixed_lesson=self.fixed,
            lesson_type=self.fixed.lesson_type, target_level=self.fixed.target_level,
            start_at=availability.start_at, end_at=availability.end_at,
        )

        with self.assertRaises(ValidationError):
            update_fixed_lesson_schedule(self._proposed(start_hour=20))

    def test_weeks_ahead_decrease_removes_only_unused_tail(self):
        old = self._occurrences()
        update_fixed_lesson_schedule(self._proposed(weeks_ahead=2))
        current = self._occurrences()
        self.assertEqual([item.pk for item in current], [item.pk for item in old[:2]])

    def test_weeks_ahead_increase_preserves_pk_and_overrides(self):
        update_fixed_lesson_schedule(self._proposed(weeks_ahead=2))
        old = self._occurrences()
        CoachAvailability.objects.filter(pk=old[0].pk).update(
            capacity=9, capacity_overridden=True,
        )

        self.fixed.refresh_from_db()
        update_fixed_lesson_schedule(self._proposed(weeks_ahead=4))

        current = self._occurrences()
        self.assertEqual([item.pk for item in current[:2]], [item.pk for item in old])
        self.assertEqual(current[0].capacity, 9)
        self.assertTrue(current[0].capacity_overridden)
        self.assertFalse(current[2].capacity_overridden)

    def test_removed_canceled_history_is_preserved_and_not_reactivated(self):
        self.fixed.members.add(self.member)
        reservation = Reservation.objects.filter(fixed_lesson=self.fixed).earliest("start_at")
        reservation.cancel(reason="会員が予約確認画面からキャンセル")
        old_availability_id = reservation.availability_id
        self.fixed.members.clear()

        update_fixed_lesson_schedule(self._proposed(weekday=(self.fixed.weekday + 1) % 7))

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.STATUS_CANCELED)
        self.assertTrue(CoachAvailability.objects.filter(pk=old_availability_id).exists())

    def test_start_hour_change_uses_model_duration(self):
        update_fixed_lesson_schedule(self._proposed(start_hour=18))
        for availability in self._occurrences():
            self.assertEqual(timezone.localtime(availability.start_at).hour, 18)
            self.assertEqual(availability.end_at - availability.start_at, timedelta(hours=2))

    def test_noop_does_not_synchronize(self):
        from unittest.mock import patch

        with patch("club.fixed_lesson_sync_facade.synchronize_fixed_lesson_membership") as sync:
            result = update_fixed_lesson_schedule(self._proposed())
        self.assertFalse(result["removed"])
        self.assertFalse(result["added"])
        sync.assert_not_called()

    def test_admin_conflict_is_a_form_error_not_500(self):
        admin_user = User.objects.create_superuser(
            username="schedule-admin", password="password", email="admin@example.com",
        )
        self.fixed.members.add(self.member)
        self.client.force_login(admin_user)
        response = self.client.post(
            reverse("admin:club_fixedlesson_change", args=[self.fixed.pk]),
            {
                "title": self.fixed.title,
                "is_active": "on",
                "lesson_type": self.fixed.lesson_type,
                "target_level": self.fixed.target_level,
                "target_level_2": "",
                "start_date": self.fixed.start_date.isoformat(),
                "weekday": str((self.fixed.weekday + 1) % 7),
                "start_hour": str(self.fixed.start_hour),
                "weeks_ahead": str(self.fixed.weeks_ahead),
                "coach": str(self.coach.pk),
                "coach_2": "",
                "coach_3": "",
                "court": str(self.court.pk),
                "coach_count": "1",
                "court_count": "1",
                "capacity": "4",
                "members": [str(self.member.pk)],
                "note": "",
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "既存予約/キャンセル待ちがあるため日程を変更できません")
        self.fixed.refresh_from_db()
        self.assertEqual(self.fixed.weekday, self.fixed.start_date.weekday())
