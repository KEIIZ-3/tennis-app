from datetime import datetime, timedelta
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club import lesson_execution
from club.lesson_execution_storage import save_status
from club.models import CoachAvailability, Court, FixedLesson, Reservation
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

    def _availability(
        self, start_at, *, duration=1, capacity=1, participant_status=Reservation.STATUS_ACTIVE
    ):
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=get_user_model().LEVEL_BEGINNER,
            start_at=start_at,
            end_at=start_at + timedelta(hours=duration),
            capacity=capacity,
            status=CoachAvailability.STATUS_OPEN,
        )
        if participant_status:
            Reservation.objects.create(
                user=self.member,
                coach=self.coach,
                court=self.court,
                availability=availability,
                lesson_type=Reservation.LESSON_PRIVATE,
                target_level=get_user_model().LEVEL_BEGINNER,
                start_at=availability.start_at,
                end_at=availability.end_at,
                status=participant_status,
            )
        return availability

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

    def test_coach_names_are_people_not_characters(self):
        self.coach.full_name = "井上春佳"
        self.coach.save(update_fields=["full_name"])
        self._availability(self.now - timedelta(hours=2))

        rows = lesson_execution.unconfirmed_execution_rows(2026, 8, now=self.now)

        self.assertEqual(rows[0]["coach_names"], ["井上春佳"])
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("club:coach_admin_settlement"), {"year": 2026, "month": 8}
        )
        self.assertContains(response, "井上春佳")
        self.assertNotContains(response, "井 / 上 / 春 / 佳")

    def test_substitute_coach_name_is_used_as_one_person(self):
        User = get_user_model()
        substitute = User.objects.create_user(
            username="substitute_coach",
            full_name="清水峻平",
            role=User.ROLE_COACH,
        )
        availability = self._availability(self.now - timedelta(hours=2))
        availability.substitute_coach = substitute
        availability.save(update_fields=["substitute_coach"])

        rows = lesson_execution.unconfirmed_execution_rows(2026, 8, now=self.now)

        self.assertEqual(rows[0]["coach_names"], ["清水峻平"])
        self.assertNotEqual(rows[0]["coach_names"], list("清水峻平"))

    def test_fixed_lesson_coaches_are_joined_between_people_only(self):
        inoue = SimpleNamespace(display_name=lambda: "井上春佳")
        shimizu = SimpleNamespace(display_name=lambda: "清水峻平")
        fixed_lesson = SimpleNamespace(all_coaches=lambda: [inoue, shimizu])

        names = lesson_execution._fixed_coach_names(fixed_lesson)

        self.assertEqual(names, ["井上春佳", "清水峻平"])
        self.assertEqual(" / ".join(names), "井上春佳 / 清水峻平")

    def test_empty_fixed_lesson_coach_keeps_placeholder(self):
        fixed_lesson = SimpleNamespace(all_coaches=lambda: [])

        self.assertEqual(lesson_execution._fixed_coach_names(fixed_lesson), ["-"])

    def test_legacy_rain_cancel_is_not_unconfirmed(self):
        availability = self._availability(
            self.now - timedelta(hours=2), participant_status=None
        )
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
        availability = self._availability(
            self.now - timedelta(hours=2), participant_status=None
        )
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
            cancellation_reason="レッスン中止による自動返却",
        )

        self.assertEqual(
            lesson_execution.unconfirmed_execution_rows(2026, 8, now=self.now),
            [],
        )

    def test_refund_lifecycle_statuses_are_not_unconfirmed(self):
        for index, status in enumerate(
            (lesson_execution.STATUS_REFUND_PENDING, lesson_execution.STATUS_REFUNDED)
        ):
            availability = self._availability(
                self.now - timedelta(days=index + 1, hours=2)
            )
            self._set_execution_status(availability, status)

        self.assertEqual(
            lesson_execution.unconfirmed_execution_rows(2026, 8, now=self.now),
            [],
        )

    def test_personal_cancel_with_active_participant_stays_unconfirmed(self):
        User = get_user_model()
        active_member = User.objects.create_user(
            username="active_settlement_member", role=User.ROLE_MEMBER
        )
        availability = self._availability(
            self.now - timedelta(hours=2), capacity=5
        )
        common = {
            "coach": self.coach,
            "court": self.court,
            "availability": availability,
            "lesson_type": Reservation.LESSON_PRIVATE,
            "target_level": User.LEVEL_BEGINNER,
            "start_at": availability.start_at,
            "end_at": availability.end_at,
        }
        Reservation.objects.create(
            user=self.member,
            status=Reservation.STATUS_CANCELED,
            cancellation_reason="会員都合",
            **common,
        )
        Reservation.objects.create(
            user=active_member,
            status=Reservation.STATUS_ACTIVE,
            **common,
        )

        rows = lesson_execution.unconfirmed_execution_rows(2026, 8, now=self.now)

        self.assertEqual([row["availability_id"] for row in rows], [availability.pk])

    def test_only_occurrences_with_active_or_pending_participants_need_confirmation(self):
        empty = self._availability(
            self.now - timedelta(days=3), participant_status=None
        )
        canceled = self._availability(
            self.now - timedelta(days=2), participant_status=Reservation.STATUS_CANCELED
        )
        active = self._availability(self.now - timedelta(days=1))
        pending = self._availability(
            self.now - timedelta(hours=2), participant_status=Reservation.STATUS_PENDING
        )

        rows = lesson_execution.unconfirmed_execution_rows(2026, 8, now=self.now)

        self.assertEqual(
            [row["availability_id"] for row in rows],
            [active.pk, pending.pk],
        )
        self.assertNotIn(empty.pk, [row["availability_id"] for row in rows])
        self.assertNotIn(canceled.pk, [row["availability_id"] for row in rows])

    def test_fixed_occurrence_replaces_predecessor_availability(self):
        target = timezone.make_aware(datetime(2026, 8, 5, 19, 0))
        fixed = FixedLesson.objects.create(
            title="Canonical fixed lesson",
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=get_user_model().LEVEL_BEGINNER,
            start_date=target.date(),
            weekday=target.weekday(),
            start_hour=target.hour,
            capacity=4,
            weeks_ahead=1,
        )
        predecessor = self._availability(target, participant_status=None)
        Reservation.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court,
            availability=predecessor,
            fixed_lesson=fixed,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=get_user_model().LEVEL_BEGINNER,
            start_at=predecessor.start_at,
            end_at=predecessor.end_at,
            status=Reservation.STATUS_ACTIVE,
        )

        rows = lesson_execution.unconfirmed_execution_rows(
            2026, 8, now=target + timedelta(days=1)
        )

        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["availability_id"], predecessor.pk)

    def test_august_audit_shape_keeps_only_occurrences_needing_confirmation(self):
        fixed_start = timezone.make_aware(datetime(2026, 8, 5, 19, 0))
        fixed = FixedLesson.objects.create(
            title="Wednesday fixed lesson",
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=get_user_model().LEVEL_BEGINNER,
            start_date=fixed_start.date(),
            weekday=fixed_start.weekday(),
            start_hour=fixed_start.hour,
            capacity=4,
            weeks_ahead=1,
        )
        predecessor = self._availability(fixed_start, participant_status=None)
        Reservation.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court,
            availability=predecessor,
            fixed_lesson=fixed,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=get_user_model().LEVEL_BEGINNER,
            start_at=predecessor.start_at,
            end_at=predecessor.end_at,
            status=Reservation.STATUS_CANCELED,
        )
        settlement = MonthlySettlement.objects.create(year=2026, month=8)
        save_status(
            settlement,
            lesson_execution._fixed_slot_key(fixed, fixed_start.date()),
            lesson_execution.STATUS_HELD,
            self.admin,
        )
        self._availability(
            timezone.make_aware(datetime(2026, 8, 9, 19, 0)),
            participant_status=Reservation.STATUS_CANCELED,
        )
        self._availability(
            timezone.make_aware(datetime(2026, 8, 21, 19, 0)),
            participant_status=None,
        )
        for day in (23, 24, 28):
            self._availability(timezone.make_aware(datetime(2026, 8, day, 19, 0)))

        rows = lesson_execution.unconfirmed_execution_rows(
            2026,
            8,
            now=timezone.make_aware(datetime(2026, 8, 29, 0, 0)),
        )

        self.assertEqual(
            [row["lesson_date"].day for row in rows],
            [23, 24, 28],
        )

    def test_cancellation_evidence_overrides_stale_held_metadata(self):
        availability = self._availability(self.now - timedelta(hours=2))
        self._set_execution_status(availability, lesson_execution.STATUS_HELD)
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
            cancellation_reason="レッスン中止による自動返却",
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
