from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club import lesson_execution
from club.lesson_execution_storage import save_status
from club.models import CoachAvailability, Court, Reservation
from club.settlement_models import MonthlySettlement


class SettlementUnconfirmedExecutionTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            username="settlement_admin",
            password="password12345",
            role=User.ROLE_COACH,
            is_staff=True,
        )
        self.coach = User.objects.create_user(
            username="settlement_coach",
            password="password12345",
            role=User.ROLE_COACH,
        )
        self.member = User.objects.create_user(
            username="settlement_member",
            password="password12345",
            role=User.ROLE_MEMBER,
        )
        self.court = Court.objects.create(
            name="未確定テストコート",
            is_active=True,
            court_type=Court.COURT_SONO,
        )
        self.now = timezone.make_aware(datetime(2026, 8, 15, 12, 0))

    def _availability(self, start_at, *, duration=1):
        return CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=get_user_model().LEVEL_BEGINNER,
            start_at=start_at,
            end_at=start_at + timedelta(hours=duration),
            capacity=1,
            status=CoachAvailability.STATUS_OPEN,
        )

    def _set_execution_status(self, availability, status):
        settlement, _ = MonthlySettlement.objects.get_or_create(
            year=availability.start_at.year,
            month=availability.start_at.month,
        )
        save_status(
            settlement,
            lesson_execution._availability_key(availability),
            status,
            self.admin,
        )

    def test_query_filters_time_month_and_confirmed_statuses(self):
        ended = self._availability(self.now - timedelta(hours=2))
        future = self._availability(self.now + timedelta(hours=1))
        held = self._availability(self.now - timedelta(days=1, hours=2))
        rain = self._availability(self.now - timedelta(days=2, hours=2))
        outside = self._availability(timezone.make_aware(datetime(2026, 7, 31, 19, 0)))
        self._set_execution_status(held, lesson_execution.STATUS_HELD)
        self._set_execution_status(rain, lesson_execution.STATUS_RAIN_CANCELED)

        rows = lesson_execution.unconfirmed_execution_rows(
            2026, 8, now=self.now
        )

        self.assertEqual([row["availability_id"] for row in rows], [ended.pk])
        self.assertNotIn(future.pk, [row["availability_id"] for row in rows])
        self.assertNotIn(outside.pk, [row["availability_id"] for row in rows])

    def test_month_boundary_and_multiple_occurrences(self):
        first = self._availability(timezone.make_aware(datetime(2026, 8, 1, 9, 0)))
        last = self._availability(timezone.make_aware(datetime(2026, 8, 31, 19, 0)))

        rows = lesson_execution.unconfirmed_execution_rows(
            2026,
            8,
            now=timezone.make_aware(datetime(2026, 9, 1, 0, 1)),
        )

        self.assertEqual(
            [row["availability_id"] for row in rows],
            [first.pk, last.pk],
        )

    def test_legacy_rain_cancel_is_not_unconfirmed(self):
        availability = self._availability(self.now - timedelta(hours=2))
        Reservation.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court,
            availability=availability,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=get_user_model().LEVEL_BEGINNER,
            start_at=availability.start_at,
            end_at=availability.end_at,
            status=Reservation.STATUS_RAIN_CANCELED,
            cancellation_reason="雨天中止",
        )

        self.assertEqual(
            lesson_execution.unconfirmed_execution_rows(2026, 8, now=self.now),
            [],
        )

    def test_explicitly_canceled_occurrence_is_not_unconfirmed(self):
        availability = self._availability(self.now - timedelta(hours=2))
        Reservation.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court,
            availability=availability,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=get_user_model().LEVEL_BEGINNER,
            start_at=availability.start_at,
            end_at=availability.end_at,
            status=Reservation.STATUS_CANCELED,
            cancellation_reason="管理者による開催取消",
        )

        self.assertEqual(
            lesson_execution.unconfirmed_execution_rows(2026, 8, now=self.now),
            [],
        )

    def test_admin_warning_link_and_zero_state(self):
        start_at = timezone.make_aware(
            datetime.combine(timezone.localdate() - timedelta(days=1), datetime.min.time()).replace(hour=10)
        )
        availability = self._availability(start_at)
        self.client.force_login(self.admin)

        response = self.client.get(
            reverse("club:coach_admin_settlement"),
            {"year": availability.start_at.year, "month": availability.start_at.month},
        )
        self.assertContains(response, "実施状態が未確定の開催回（1件）")
        self.assertContains(response, "実施状態を確認")
        self.assertContains(response, f"#lesson-{availability.pk}")

        self._set_execution_status(availability, lesson_execution.STATUS_HELD)
        response = self.client.get(
            reverse("club:coach_admin_settlement"),
            {"year": availability.start_at.year, "month": availability.start_at.month},
        )
        self.assertEqual(response.context["unconfirmed_execution_count"], 0)
        self.assertNotContains(response, "実施状態が未確定の開催回（")

    def test_non_admin_cannot_view_settlement_warning(self):
        start_at = timezone.make_aware(
            datetime.combine(timezone.localdate() - timedelta(days=1), datetime.min.time()).replace(hour=10)
        )
        self._availability(start_at)
        self.client.force_login(self.coach)

        response = self.client.get(reverse("club:coach_admin_settlement"))

        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, "実施状態が未確定", status_code=403)

    def test_close_month_is_rejected_while_execution_is_unconfirmed(self):
        start_at = timezone.make_aware(
            datetime.combine(timezone.localdate() - timedelta(days=1), datetime.min.time()).replace(hour=10)
        )
        availability = self._availability(start_at)
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("club:coach_admin_settlement"),
            {
                "action": "close_month",
                "year": availability.start_at.year,
                "month": availability.start_at.month,
            },
        )

        self.assertEqual(response.status_code, 302)
        settlement = MonthlySettlement.objects.get(
            year=availability.start_at.year,
            month=availability.start_at.month,
        )
        self.assertFalse(settlement.is_closed)

    def test_closed_month_get_preserves_snapshot(self):
        snapshot = {"sentinel": "unchanged", "closing_balance": 1234}
        self._availability(self.now - timedelta(hours=2))
        settlement = MonthlySettlement.objects.create(
            year=2026,
            month=8,
            status=MonthlySettlement.STATUS_CLOSED,
            calculation_snapshot=snapshot,
            closed_at=timezone.now(),
            closed_by=self.admin,
        )
        self.client.force_login(self.admin)

        response = self.client.get(
            reverse("club:coach_admin_settlement"),
            {"year": 2026, "month": 8},
        )

        self.assertEqual(response.status_code, 200)
        settlement.refresh_from_db()
        self.assertEqual(settlement.calculation_snapshot, snapshot)
        self.assertTrue(settlement.is_closed)
        self.assertEqual(response.context["unconfirmed_execution_count"], 0)
