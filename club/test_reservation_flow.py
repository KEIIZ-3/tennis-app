from datetime import date, datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from . import admin_dashboard, lesson_execution
from .admin import StringingOrderAdminForm
from .models import (
    CoachAvailability,
    CoachExpense,
    Court,
    FixedLesson,
    LessonWaitlist,
    LessonWaitlistParticipant,
    Reservation,
    StringingOrder,
    TicketLedger,
    TicketConsumption,
    TicketPurchase,
)
from .settlement_models import MonthlySettlement


class ReservationFlowSmokeTests(TestCase):
    """
    予約導線の最低限の自動デバッグ用テストです。

    目的：
    - lesson-calendar が 500 にならないこと
    - 予約前確認URLが解決できること
    - 会員が通常レッスンを予約できること
    - 業務委託コーチが他コーチ担当レッスンを受講予約できること
    - 業務委託コーチが自分担当レッスンを予約できないこと
    - 2026年7月プレオープンはチケットを消費しないこと
    - 満員時にキャンセル待ち登録できること
    """

    def setUp(self):
        self.User = get_user_model()
        self.client = Client()

        self.court = Court.objects.create(
            name="テストコート",
            is_active=True,
            court_type=Court.COURT_SONO,
        )

        self.member = self._create_user(
            username="member_test",
            role=self.User.ROLE_MEMBER,
            full_name="会員 テスト",
            ticket_balance=0,
        )

        self.coach = self._create_user(
            username="coach_test",
            role=self.User.ROLE_COACH,
            full_name="飯塚 コーチ",
            ticket_balance=0,
        )

        self.contractor = self._create_user(
            username="contractor_test",
            role=self.User.ROLE_CONTRACTOR_COACH,
            full_name="業務委託 コーチ",
            ticket_balance=0,
        )

        self.lesson_date = timezone.localdate() + timedelta(days=7)

    def _create_user(self, *, username, role, full_name, ticket_balance=0):
        user = self.User.objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password="password12345",
        )
        user.role = role
        user.full_name = full_name
        user.phone_number = "09000000000"
        user.is_profile_completed = True
        user.member_level = self.User.LEVEL_BEGINNER
        user.ticket_balance = ticket_balance
        user.save()
        return user

    def _create_fixed_lesson(self, *, coach=None, lesson_date=None, title="テスト一般レッスン"):
        target_date = lesson_date or self.lesson_date
        return FixedLesson.objects.create(
            title=title,
            coach=coach or self.coach,
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            target_level_2="",
            start_date=target_date,
            weekday=target_date.weekday(),
            start_hour=19,
            capacity=6,
            coach_count=1,
            court_count=1,
            weeks_ahead=1,
            is_active=True,
        )

    def _post_lesson_calendar_reserve(self, *, user, fixed_lesson, lesson_date=None, action="reserve"):
        self.client.force_login(user)
        target_date = lesson_date or self.lesson_date
        return self.client.post(
            reverse("club:lesson_calendar"),
            data={
                "action": action,
                "fixed_lesson_id": str(fixed_lesson.pk),
                "lesson_date": target_date.isoformat(),
                "year": str(target_date.year),
                "month": str(target_date.month),
            },
        )

    def test_private_operation_endpoints_require_login(self):
        endpoints = (
            ("get", reverse("club:stringing_order_detail", args=[999999])),
            ("post", reverse("club:reservation_cancel", args=[999999])),
            ("get", reverse("club:coach_availability_list")),
        )
        for method, url in endpoints:
            with self.subTest(url=url):
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith(reverse("club:login")))

    def test_contractor_only_sees_assigned_stringing_orders(self):
        other_contractor = self._create_user(
            username="other_contractor",
            role=self.User.ROLE_CONTRACTOR_COACH,
            full_name="別担当 コーチ",
        )
        own_customer = self._create_user(
            username="own_stringing_customer",
            role=self.User.ROLE_MEMBER,
            full_name="担当 顧客",
        )
        other_customer = self._create_user(
            username="other_stringing_customer",
            role=self.User.ROLE_MEMBER,
            full_name="担当外 顧客",
        )
        StringingOrder.objects.create(
            user=own_customer,
            assigned_coach=self.contractor,
            racket_name="担当ラケット",
            preferred_delivery_time="担当納期",
        )
        StringingOrder.objects.create(
            user=other_customer,
            assigned_coach=other_contractor,
            racket_name="担当外ラケット",
            preferred_delivery_time="担当外納期",
        )

        self.client.force_login(self.contractor)
        response = self.client.get(reverse("club:stringing_order_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "担当ラケット")
        self.assertNotContains(response, "担当外ラケット")
        self.assertNotContains(response, "担当外 顧客")

    def test_contractor_cannot_view_business_revenue_summary(self):
        self.client.force_login(self.contractor)

        response = self.client.get(reverse("club:coach_revenue_summary"))

        self.assertEqual(response.status_code, 403)

    def test_contractor_cannot_view_business_analytics(self):
        self.client.force_login(self.contractor)

        response = self.client.get(reverse("club:analytics_dashboard"))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("club:login")))

    def test_contractor_navigation_links_to_own_payroll(self):
        self.client.force_login(self.contractor)

        response = self.client.get(reverse("club:home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("club:coach_payroll_summary"))
        self.assertNotContains(response, reverse("club:coach_revenue_summary"))
        self.assertNotContains(response, reverse("club:coach_admin_settlement"))

    def test_staff_coach_can_switch_payroll_to_another_coach(self):
        self.coach.is_staff = True
        self.coach.save(update_fields=["is_staff"])
        self.client.force_login(self.coach)

        response = self.client.get(
            reverse("club:coach_payroll_summary"),
            data={
                "year": 2026,
                "month": 7,
                "coach_id": self.contractor.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_admin_mode"])
        self.assertEqual(response.context["selected_coach"], self.contractor)
        self.assertEqual(
            response.context["selected_coach_id"],
            str(self.contractor.pk),
        )
        self.assertEqual(response.context["row"]["coach"], self.contractor)

    def test_regular_coach_cannot_switch_payroll_to_another_coach(self):
        self.client.force_login(self.coach)

        response = self.client.get(
            reverse("club:coach_payroll_summary"),
            data={
                "year": 2026,
                "month": 7,
                "coach_id": self.contractor.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["is_admin_mode"])
        self.assertEqual(response.context["selected_coach"], self.coach)
        self.assertEqual(response.context["selected_coach_id"], str(self.coach.pk))

    def test_contractor_cannot_view_other_coach_lesson_members(self):
        fixed_lesson = self._create_fixed_lesson(coach=self.coach)
        fixed_lesson.members.add(self.member)
        self.client.force_login(self.contractor)

        response = self.client.get(
            reverse("club:lesson_calendar_member_list"),
            data={
                "fixed_lesson_id": fixed_lesson.pk,
                "lesson_date": self.lesson_date.isoformat(),
            },
        )

        self.assertEqual(response.status_code, 403)

    def test_contractor_today_lessons_is_limited_to_own_lessons(self):
        own_lesson = self._create_fixed_lesson(
            coach=self.contractor,
            title="業務委託担当レッスン",
        )
        other_lesson = self._create_fixed_lesson(
            coach=self.coach,
            title="担当外レッスン",
        )
        own_lesson.members.add(self.member)
        other_lesson.members.add(self.member)
        self.client.force_login(self.contractor)

        response = self.client.get(
            reverse("club:coach_today_lessons"),
            data={"days": "14", "coach_id": self.coach.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "業務委託担当レッスン")
        self.assertNotContains(response, "担当外レッスン")
        self.assertEqual(response.context["selected_coach_id"], str(self.contractor.pk))
        self.assertFalse(response.context["is_staff_mode"])

    def test_deleting_fixed_lesson_cancels_reservations_and_generated_slot(self):
        lesson = self._create_fixed_lesson()
        lesson.members.add(self.member)
        lesson.sync_future_reservations(created_by=self.coach)
        reservation = Reservation.objects.get(fixed_lesson=lesson, user=self.member)
        availability_id = reservation.availability_id

        lesson.delete(created_by=self.coach)

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.STATUS_CANCELED)
        self.assertEqual(reservation.cancellation_reason, "固定レッスン削除による取消")
        self.assertFalse(CoachAvailability.objects.filter(pk=availability_id).exists())

    def test_today_lessons_hides_legacy_orphaned_fixed_lesson_slot(self):
        lesson_date = self.lesson_date
        lesson = self._create_fixed_lesson(
            lesson_date=lesson_date,
            title="削除済み固定レッスン",
        )
        lesson.members.add(self.member)
        lesson.sync_future_reservations(created_by=self.coach)
        availability = CoachAvailability.objects.get(
            reservations__fixed_lesson=lesson,
        )

        # 旧実装の削除後状態（FixedLessonだけ消え、生成枠と予約が残存）を再現する。
        FixedLesson.objects.filter(pk=lesson.pk).delete()
        self.assertTrue(CoachAvailability.objects.filter(pk=availability.pk).exists())

        self.client.force_login(self.coach)
        with patch("club.views.timezone.localdate", return_value=lesson_date):
            response = self.client.get(
                reverse("club:coach_today_lessons"),
                data={"days": "1"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary"]["today_lesson_count"], 0)
        self.assertNotContains(response, "削除済み固定レッスン")

    def test_contractor_cannot_execute_other_coach_lesson(self):
        own_lesson = self._create_fixed_lesson(
            coach=self.contractor,
            title="業務委託実施対象",
        )
        other_lesson = self._create_fixed_lesson(
            coach=self.coach,
            title="担当外実施対象",
        )
        own_start, own_end = own_lesson._build_datetimes_for_date(self.lesson_date)
        other_start, other_end = other_lesson._build_datetimes_for_date(self.lesson_date)
        own_availability = lesson_execution._canonical_availability_for_fixed(
            own_lesson,
            own_start,
            own_end,
        )
        other_availability = lesson_execution._canonical_availability_for_fixed(
            other_lesson,
            other_start,
            other_end,
        )
        self.client.force_login(self.contractor)

        list_response = self.client.get(
            reverse("club:lesson_execution_manage"),
            data={"year": self.lesson_date.year, "month": self.lesson_date.month},
        )
        visible_availability_ids = {
            row["availability"].pk for row in list_response.context["rows"]
        }
        self.assertIn(own_availability.pk, visible_availability_ids)
        self.assertNotIn(other_availability.pk, visible_availability_ids)

        action_response = self.client.post(
            reverse("club:lesson_execution_manage"),
            data={
                "year": self.lesson_date.year,
                "month": self.lesson_date.month,
                "availability_id": other_availability.pk,
                "action": lesson_execution.STATUS_RAIN_CANCELED,
            },
        )
        self.assertEqual(action_response.status_code, 403)

    def test_substitute_contractor_can_rain_cancel_assigned_availability(self):
        start_at = timezone.make_aware(
            datetime.combine(self.lesson_date, datetime.min.time()).replace(hour=10)
        )
        end_at = start_at + timedelta(hours=1)
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            substitute_coach=self.contractor,
            court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=end_at,
            capacity=1,
            status=CoachAvailability.STATUS_OPEN,
        )
        reservation = Reservation.objects.create(
            user=self.member,
            coach=self.coach,
            substitute_coach=self.contractor,
            court=self.court,
            availability=availability,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=end_at,
            status=Reservation.STATUS_ACTIVE,
        )
        self.client.force_login(self.contractor)

        list_response = self.client.get(reverse("club:coach_availability_list"))
        self.assertEqual(list_response.status_code, 200)
        visible_ids = {
            row["availability"].pk
            for row in list_response.context["availability_rows"]
        }
        self.assertIn(availability.pk, visible_ids)

        action_response = self.client.post(
            reverse("club:coach_availability_list"),
            data={
                "action": "rain_cancel_slot",
                "availability_id": availability.pk,
            },
        )
        self.assertEqual(action_response.status_code, 302)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.STATUS_RAIN_CANCELED)

    def test_substitute_contractor_sees_fixed_lesson_weekly(self):
        fixed_lesson = self._create_fixed_lesson(
            coach=self.coach,
            title="代行担当固定レッスン",
        )
        fixed_lesson.members.add(self.member)
        start_at, end_at = fixed_lesson._build_datetimes_for_date(self.lesson_date)
        CoachAvailability.objects.create(
            coach=self.coach,
            substitute_coach=self.contractor,
            court=self.court,
            lesson_type=fixed_lesson.lesson_type,
            target_level=fixed_lesson.target_level,
            start_at=start_at,
            end_at=end_at,
            capacity=fixed_lesson.capacity,
            status=CoachAvailability.STATUS_OPEN,
        )
        self.client.force_login(self.contractor)

        response = self.client.get(reverse("club:coach_fixed_lesson_weekly"))

        self.assertEqual(response.status_code, 200)
        visible_fixed_lesson_ids = {
            row["fixed_lesson"].pk for row in response.context["fixed_lessons"]
        }
        self.assertIn(fixed_lesson.pk, visible_fixed_lesson_ids)
        self.assertContains(response, self.member.display_name())

    def test_execution_list_tracks_missing_and_no_court_cost(self):
        lesson_date = timezone.localdate() - timedelta(days=1)
        start_at = timezone.make_aware(
            datetime.combine(
                lesson_date,
                datetime.min.time(),
            ).replace(hour=10)
        )
        end_at = start_at + timedelta(hours=1)
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=end_at,
            capacity=1,
            status=CoachAvailability.STATUS_OPEN,
        )
        Reservation.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court,
            availability=availability,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=end_at,
            status=Reservation.STATUS_ACTIVE,
        )
        self.client.force_login(self.coach)

        held_response = self.client.post(
            reverse("club:lesson_execution_manage"),
            data={
                "year": lesson_date.year,
                "month": lesson_date.month,
                "availability_id": availability.pk,
                "action": lesson_execution.STATUS_HELD,
            },
        )
        self.assertEqual(held_response.status_code, 302)

        pending_response = self.client.get(
            reverse("club:lesson_execution_manage"),
            data={
                "year": lesson_date.year,
                "month": lesson_date.month,
                "pending": "1",
            },
        )
        self.assertEqual(pending_response.status_code, 200)
        self.assertEqual(
            pending_response.context["counts"]["court_unregistered"],
            1,
        )
        self.assertEqual(pending_response.context["visible_row_count"], 1)
        self.assertEqual(
            pending_response.context["rows"][0]["court_status"],
            "unregistered",
        )

        no_cost_response = self.client.post(
            reverse("club:lesson_execution_manage"),
            data={
                "year": lesson_date.year,
                "month": lesson_date.month,
                "availability_id": availability.pk,
                "action": "court_not_required",
                "pending": "1",
            },
        )
        self.assertEqual(no_cost_response.status_code, 302)
        expense = CoachExpense.objects.get(
            category=CoachExpense.CATEGORY_COURT,
            expense_date=lesson_date,
        )
        self.assertEqual(expense.amount, 0)
        self.assertIn('"court_cost_not_required": true', expense.note)

        updated_response = self.client.get(
            reverse("club:lesson_execution_manage"),
            data={
                "year": lesson_date.year,
                "month": lesson_date.month,
            },
        )
        self.assertEqual(updated_response.status_code, 200)
        self.assertEqual(
            updated_response.context["counts"]["court_unregistered"],
            0,
        )
        self.assertEqual(
            updated_response.context["rows"][0]["court_status"],
            "not_required",
        )

    def test_substitute_contractor_is_in_dashboard_slot_scope(self):
        fixed_lesson = self._create_fixed_lesson(coach=self.coach)
        start_at, end_at = fixed_lesson._build_datetimes_for_date(self.lesson_date)
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            substitute_coach=self.contractor,
            court=self.court,
            lesson_type=fixed_lesson.lesson_type,
            target_level=fixed_lesson.target_level,
            start_at=start_at,
            end_at=end_at,
            capacity=fixed_lesson.capacity,
            status=CoachAvailability.STATUS_OPEN,
        )

        self.assertTrue(
            admin_dashboard._slot_is_in_scope(
                {"fixed_lesson": fixed_lesson, "availability": availability},
                self.contractor,
            )
        )

    def test_contractor_dashboard_hides_global_company_metrics(self):
        StringingOrder.objects.create(
            user=self.member,
            assigned_coach=None,
            racket_name="未割当ラケット",
            preferred_delivery_time="来週末",
        )
        self.client.force_login(self.contractor)

        response = self.client.get(reverse("club:admin_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["stats"]["member_count"])
        self.assertIsNone(response.context["stats"]["low_ticket_member_count"])
        self.assertIsNone(response.context["stats"]["unhandled_stringing_orders"])
        self.assertNotContains(response, "チケット0枚以下")
        self.assertNotContains(response, "未完了のガット張り")
        self.assertNotContains(response, "分析ダッシュボード")
        self.assertNotContains(response, "収支管理")
        self.assertNotContains(response, "月次精算")
        self.assertNotContains(response, "経費・コート代")
        self.assertNotContains(response, "現在の有効会員は")

    def test_dashboard_does_not_double_count_same_normal_and_substitute_coach(self):
        target_date = timezone.localdate() + timedelta(days=1)
        start_at = timezone.make_aware(datetime.combine(target_date, datetime.min.time().replace(hour=10)))
        end_at = start_at + timedelta(hours=1)
        Reservation.objects.create(
            user=self.member,
            coach=self.contractor,
            court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=end_at,
            status=Reservation.STATUS_PENDING,
        )
        Reservation.objects.create(
            user=self.member,
            coach=self.coach,
            substitute_coach=self.contractor,
            court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at + timedelta(hours=1),
            end_at=end_at + timedelta(hours=1),
            status=Reservation.STATUS_PENDING,
        )
        self.client.force_login(self.contractor)

        response = self.client.get(reverse("club:admin_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["stats"]["pending_reservations"], 2)

    def test_contractor_ticket_summary_ignores_coach_id_and_refunded_tickets(self):
        other_member = self._create_user(
            username="other_ticket_member",
            role=self.User.ROLE_MEMBER,
            full_name="担当外 チケット会員",
        )
        target_date = timezone.localdate().replace(day=1)
        start_at = timezone.make_aware(
            datetime.combine(target_date, datetime.min.time().replace(hour=10))
        )
        own_reservation = Reservation.objects.create(
            user=self.member,
            coach=self.coach,
            substitute_coach=self.contractor,
            court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=start_at + timedelta(hours=1),
            status=Reservation.STATUS_ACTIVE,
        )
        other_reservation = Reservation.objects.create(
            user=other_member,
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at + timedelta(hours=1),
            end_at=start_at + timedelta(hours=2),
            status=Reservation.STATUS_ACTIVE,
        )
        own_purchase = TicketPurchase.objects.create(
            user=self.member,
            purchase_type=TicketPurchase.PURCHASE_TYPE_SINGLE,
            total_tickets=3,
            remaining_tickets=0,
            unit_price=4000,
        )
        other_purchase = TicketPurchase.objects.create(
            user=other_member,
            purchase_type=TicketPurchase.PURCHASE_TYPE_SINGLE,
            total_tickets=3,
            remaining_tickets=0,
            unit_price=4000,
        )
        TicketConsumption.objects.create(
            user=self.member,
            purchase=own_purchase,
            reservation=own_reservation,
            tickets_used=1,
            unit_price_snapshot=4000,
        )
        TicketConsumption.objects.create(
            user=self.member,
            purchase=own_purchase,
            reservation=own_reservation,
            tickets_used=2,
            unit_price_snapshot=4000,
            refunded_at=timezone.now(),
        )
        TicketConsumption.objects.create(
            user=other_member,
            purchase=other_purchase,
            reservation=other_reservation,
            tickets_used=3,
            unit_price_snapshot=4000,
        )
        self.client.force_login(self.contractor)

        response = self.client.get(
            reverse("club:coach_ticket_summary"),
            data={
                "year": target_date.year,
                "month": target_date.month,
                "coach_id": self.coach.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_coach_id"], str(self.contractor.pk))
        self.assertFalse(response.context["can_select_coach"])
        self.assertEqual(response.context["total_tickets"], 1)
        self.assertEqual(response.context["total_amount"], 4000)
        self.assertContains(response, self.member.display_name())
        self.assertNotContains(response, other_member.display_name())

        self.client.force_login(self.coach)
        normal_coach_response = self.client.get(
            reverse("club:coach_ticket_summary"),
            data={"year": target_date.year, "month": target_date.month},
        )
        self.assertEqual(normal_coach_response.context["total_tickets"], 3)
        self.assertNotContains(normal_coach_response, self.member.display_name())
        self.assertContains(normal_coach_response, other_member.display_name())

    def test_main_coach_can_select_another_coach_in_ticket_summary(self):
        main_coach = self._create_user(
            username="main_ticket_coach",
            role=self.User.ROLE_COACH,
            full_name="飯塚研太朗",
        )
        self.client.force_login(main_coach)

        response = self.client.get(
            reverse("club:coach_ticket_summary"),
            data={"coach_id": self.contractor.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_select_coach"])
        self.assertEqual(response.context["selected_coach_id"], str(self.contractor.pk))
        self.assertEqual(response.context["selected_coach"], self.contractor)

    def test_closed_month_blocks_reservation_refund_and_expense_changes(self):
        target_date = timezone.localdate().replace(day=1)
        start_at = timezone.make_aware(
            datetime.combine(target_date, datetime.min.time().replace(hour=10))
        )
        self.member.ticket_balance = 4
        self.member.save(update_fields=["ticket_balance"])
        reservation = Reservation.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=start_at + timedelta(hours=1),
            status=Reservation.STATUS_ACTIVE,
        )
        reservation.consume_tickets(created_by=self.member)
        existing_expense = CoachExpense.objects.create(
            expense_date=target_date,
            category=CoachExpense.CATEGORY_OTHER,
            amount=1000,
            created_by=self.coach,
        )
        MonthlySettlement.objects.create(
            year=target_date.year,
            month=target_date.month,
            status=MonthlySettlement.STATUS_CLOSED,
        )

        with self.assertRaisesMessage(ValidationError, "締め済みの月"):
            reservation.cancel(created_by=self.member)
        reservation.refresh_from_db()
        self.member.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.STATUS_ACTIVE)
        self.assertIsNone(reservation.ticket_refunded_at)
        self.assertEqual(self.member.ticket_balance, 2)

        with self.assertRaisesMessage(ValidationError, "締め済みの月"):
            CoachExpense.objects.create(
                expense_date=target_date,
                category=CoachExpense.CATEGORY_OTHER,
                amount=2000,
                created_by=self.coach,
            )

        existing_expense.amount = 3000
        with self.assertRaisesMessage(ValidationError, "締め済みの月"):
            existing_expense.save(update_fields=["amount"])
        existing_expense.refresh_from_db()
        self.assertEqual(existing_expense.amount, 1000)

    def test_closed_month_court_transfer_endpoint_does_not_create_expense(self):
        self.coach.full_name = "飯塚研太朗"
        self.coach.save(update_fields=["full_name"])
        target_date = timezone.localdate().replace(day=1)
        start_at = timezone.make_aware(
            datetime.combine(target_date, datetime.min.time().replace(hour=10))
        )
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            substitute_coach=self.contractor,
            court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=start_at + timedelta(hours=1),
            capacity=1,
            status=CoachAvailability.STATUS_OPEN,
        )
        MonthlySettlement.objects.create(
            year=target_date.year,
            month=target_date.month,
            status=MonthlySettlement.STATUS_CLOSED,
        )
        self.client.force_login(self.contractor)

        response = self.client.post(
            reverse("club:coach_expense_manage"),
            data={
                "action": "create_court_transfer",
                "availability_id": availability.pk,
                "payer_coach_id": self.coach.pk,
                "amount": "3000",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(CoachExpense.objects.exists())

    def test_revenue_uses_current_fixed_lesson_coach_for_stale_reservations(self):
        inoue = self._create_user(
            username="inoue_revenue",
            role=self.User.ROLE_COACH,
            full_name="井上春佳",
        )
        kazue = self._create_user(
            username="kazue_revenue",
            role=self.User.ROLE_MEMBER,
            full_name="楊和枝",
        )
        mitsunori = self._create_user(
            username="mitsunori_revenue",
            role=self.User.ROLE_MEMBER,
            full_name="矢野充則",
        )
        lesson_date = date(2026, 7, 16)
        fixed_lesson = self._create_fixed_lesson(
            coach=inoue,
            lesson_date=lesson_date,
            title="7月16日井上コーチレッスン",
        )
        start_at, end_at = fixed_lesson._build_datetimes_for_date(lesson_date)
        availability = CoachAvailability.objects.create(
            coach=inoue,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=end_at,
            capacity=6,
            status=CoachAvailability.STATUS_OPEN,
        )

        reservations = []
        for member in (kazue, mitsunori):
            reservations.append(
                Reservation.objects.create(
                    user=member,
                    coach=inoue,
                    court=self.court,
                    availability=availability,
                    fixed_lesson=fixed_lesson,
                    lesson_type=Reservation.LESSON_GENERAL,
                    target_level=self.User.LEVEL_BEGINNER,
                    start_at=start_at,
                    end_at=end_at,
                    status=Reservation.STATUS_ACTIVE,
                    payment_status=Reservation.PAYMENT_STATUS_PAID,
                    payment_amount=2000,
                )
            )

        Reservation.objects.filter(pk=reservations[0].pk).update(
            coach=self.coach,
        )
        self.client.force_login(self.coach)

        response = self.client.get(
            reverse("club:coach_revenue_summary"),
            data={"year": 2026, "month": 7},
        )

        self.assertEqual(response.status_code, 200)
        rows_by_coach = {
            row["coach_name"]: row
            for row in response.context["coach_sales_rows"]
        }
        self.assertEqual(rows_by_coach["井上春佳"]["reservation_count"], 2)
        self.assertEqual(rows_by_coach["井上春佳"]["total_amount"], 4000)
        self.assertNotIn(self.coach.display_name(), rows_by_coach)
        self.assertContains(response, "楊和枝")
        self.assertContains(response, "矢野充則")

    def test_revenue_excludes_reservation_from_deleted_fixed_lesson(self):
        lesson_date = date(2026, 7, 19)
        fixed_lesson = self._create_fixed_lesson(
            lesson_date=lesson_date,
            title="削除済み7月19日レッスン",
        )
        start_at, end_at = fixed_lesson._build_datetimes_for_date(lesson_date)
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=end_at,
            capacity=6,
            status=CoachAvailability.STATUS_OPEN,
            note="固定レッスン: 削除済み7月19日レッスン",
        )
        reservation = Reservation.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court,
            availability=availability,
            fixed_lesson=fixed_lesson,
            is_fixed_entry=True,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=end_at,
            status=Reservation.STATUS_ACTIVE,
            payment_status=Reservation.PAYMENT_STATUS_UNPAID,
            payment_amount=2000,
        )

        # 修正前に発生した、固定レッスンだけ削除され予約と生成枠が残る状態。
        FixedLesson.objects.filter(pk=fixed_lesson.pk).delete()
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.STATUS_ACTIVE)
        self.assertIsNone(reservation.fixed_lesson_id)

        self.client.force_login(self.coach)
        response = self.client.get(
            reverse("club:coach_revenue_summary"),
            data={"year": 2026, "month": 7},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["lesson_sales_total"], 0)
        self.assertEqual(response.context["preopen_unpaid_total"], 0)
        self.assertNotContains(response, "削除済み7月19日レッスン")

        payroll_response = self.client.get(
            reverse("club:coach_payroll_summary"),
            data={"year": 2026, "month": 7},
        )
        self.assertEqual(payroll_response.status_code, 200)
        self.assertEqual(
            payroll_response.context["row"]["preopen_unpaid_amount"],
            0,
        )
        self.assertEqual(payroll_response.context["salary_due"], 0)

    def test_revenue_reconciles_paid_unpaid_and_waived_preopen_fees(self):
        lesson_date = date(2026, 7, 16)
        fixed_lesson = self._create_fixed_lesson(lesson_date=lesson_date)
        start_at, end_at = fixed_lesson._build_datetimes_for_date(lesson_date)
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=end_at,
            capacity=6,
            status=CoachAvailability.STATUS_OPEN,
        )
        statuses = (
            Reservation.PAYMENT_STATUS_PAID,
            Reservation.PAYMENT_STATUS_UNPAID,
            Reservation.PAYMENT_STATUS_WAIVED,
        )
        for index, payment_status in enumerate(statuses):
            member = self._create_user(
                username=f"revenue_status_{index}",
                role=self.User.ROLE_MEMBER,
                full_name=f"集計会員{index}",
            )
            Reservation.objects.create(
                user=member,
                coach=self.coach,
                court=self.court,
                availability=availability,
                fixed_lesson=fixed_lesson,
                lesson_type=Reservation.LESSON_GENERAL,
                target_level=self.User.LEVEL_BEGINNER,
                start_at=start_at,
                end_at=end_at,
                status=Reservation.STATUS_ACTIVE,
                payment_status=payment_status,
                payment_amount=2000,
            )

        self.client.force_login(self.coach)
        response = self.client.get(
            reverse("club:coach_revenue_summary"),
            data={"year": 2026, "month": 7},
        )

        self.assertEqual(response.context["preopen_paid_total"], 2000)
        self.assertEqual(response.context["preopen_unpaid_total"], 2000)
        self.assertEqual(response.context["preopen_waived_total"], 2000)
        self.assertEqual(response.context["preopen_sales_total"], 4000)
        self.assertEqual(response.context["lesson_sales_total"], 4000)
        self.assertEqual(response.context["cash_basis_total"], 2000)

    def test_contractor_cannot_be_assigned_to_stringing_order(self):
        main_coach = self._create_user(
            username="main_stringing_coach",
            role=self.User.ROLE_COACH,
            full_name="清水峻平",
        )
        unlisted_coach = self._create_user(
            username="unlisted_stringing_coach",
            role=self.User.ROLE_COACH,
            full_name="候補外 コーチ",
        )
        non_stringing_main_coach = self._create_user(
            username="non_stringing_main_coach",
            role=self.User.ROLE_COACH,
            full_name="井上春佳",
        )
        order = StringingOrder(
            user=self.member,
            assigned_coach=self.contractor,
            racket_name="テストラケット",
            preferred_delivery_time="来週末",
        )

        with self.assertRaisesMessage(
            ValidationError,
            "ガット張りの担当者には対応可能なコーチを指定してください。",
        ):
            order.full_clean()

        order.assigned_coach = unlisted_coach
        with self.assertRaisesMessage(
            ValidationError,
            "ガット張りの担当者には対応可能なコーチを指定してください。",
        ):
            order.full_clean()

        order.assigned_coach = non_stringing_main_coach
        with self.assertRaisesMessage(
            ValidationError,
            "ガット張りの担当者には対応可能なコーチを指定してください。",
        ):
            order.full_clean()

        assigned_coach_queryset = StringingOrderAdminForm().fields[
            "assigned_coach"
        ].queryset
        self.assertIn(main_coach, assigned_coach_queryset)
        self.assertNotIn(non_stringing_main_coach, assigned_coach_queryset)
        self.assertNotIn(unlisted_coach, assigned_coach_queryset)
        self.assertNotIn(self.contractor, assigned_coach_queryset)

    def test_lesson_calendar_page_does_not_return_500_for_member(self):
        self._create_fixed_lesson()
        self.client.force_login(self.member)

        response = self.client.get(
            reverse("club:lesson_calendar"),
            data={"year": "2026", "month": "7"},
        )

        self.assertEqual(response.status_code, 200)

    def test_lesson_reservation_confirm_url_is_available(self):
        fixed_lesson = self._create_fixed_lesson()
        self.client.force_login(self.member)

        url = reverse("club:lesson_reservation_confirm")
        response = self.client.get(
            url,
            data={
                "fixed_lesson_id": str(fixed_lesson.pk),
                "lesson_date": self.lesson_date.isoformat(),
                "year": "2026",
                "month": "7",
            },
        )

        self.assertNotEqual(response.status_code, 500)

    def test_lesson_execution_creates_one_canonical_availability(self):
        fixed_lesson = self._create_fixed_lesson()
        fixed_lesson.court = None
        fixed_lesson.save(update_fields=["court"])
        start_at, end_at = fixed_lesson._build_datetimes_for_date(
            self.lesson_date,
        )

        first = lesson_execution._canonical_availability_for_fixed(
            fixed_lesson,
            start_at,
            end_at,
        )
        second = lesson_execution._canonical_availability_for_fixed(
            fixed_lesson,
            start_at,
            end_at,
        )

        self.assertIsNotNone(first)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.court_id, self.court.pk)
        self.assertEqual(first.capacity, fixed_lesson.effective_capacity())
        self.assertEqual(
            CoachAvailability.objects.filter(
                coach=self.coach,
                court=self.court,
                lesson_type=fixed_lesson.lesson_type,
                start_at=start_at,
                end_at=end_at,
            ).count(),
            1,
        )

    def test_member_can_reserve_regular_preopen_lesson_without_ticket_consumption(self):
        preopen_date = date(2026, 7, 3)
        fixed_lesson = self._create_fixed_lesson(lesson_date=preopen_date)
        mocked_now = timezone.make_aware(datetime(2026, 7, 2, 12, 0))

        with patch("django.utils.timezone.now", return_value=mocked_now):
            response = self._post_lesson_calendar_reserve(
                user=self.member,
                fixed_lesson=fixed_lesson,
                lesson_date=preopen_date,
            )

        self.assertEqual(response.status_code, 302)

        reservation = Reservation.objects.get(user=self.member, fixed_lesson=fixed_lesson)
        self.assertEqual(reservation.status, Reservation.STATUS_ACTIVE)
        self.assertEqual(reservation.tickets_used, 0)

        self.member.refresh_from_db()
        self.assertEqual(self.member.ticket_balance, 0)

    def test_preopen_level_exception_uses_actual_lesson_date(self):
        preopen_date = date(2026, 7, 3)
        fixed_lesson = self._create_fixed_lesson(lesson_date=preopen_date)
        fixed_lesson.target_level = self.User.LEVEL_ADVANCED
        fixed_lesson.save(update_fields=["target_level"])
        mocked_now = timezone.make_aware(datetime(2026, 7, 2, 12, 0))

        with patch("django.utils.timezone.now", return_value=mocked_now):
            response = self._post_lesson_calendar_reserve(
                user=self.member,
                fixed_lesson=fixed_lesson,
                lesson_date=preopen_date,
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Reservation.objects.filter(user=self.member, fixed_lesson=fixed_lesson).exists())

    def test_preopen_query_parameters_cannot_bypass_level_for_august_lesson(self):
        lesson_date = date(2026, 8, 7)
        fixed_lesson = self._create_fixed_lesson(lesson_date=lesson_date)
        fixed_lesson.target_level = self.User.LEVEL_ADVANCED
        fixed_lesson.save(update_fields=["target_level"])
        mocked_now = timezone.make_aware(datetime(2026, 8, 6, 12, 0))
        self.client.force_login(self.member)

        with patch("django.utils.timezone.now", return_value=mocked_now):
            response = self.client.post(
                reverse("club:lesson_calendar"),
                data={
                    "action": "reserve",
                    "fixed_lesson_id": fixed_lesson.pk,
                    "lesson_date": lesson_date.isoformat(),
                    "year": "2026",
                    "month": "7",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Reservation.objects.filter(user=self.member, fixed_lesson=fixed_lesson).exists())

    def test_stale_duplicate_cancel_refunds_tickets_only_once(self):
        lesson_date = date(2026, 8, 7)
        self.member.ticket_balance = 10
        self.member.save(update_fields=["ticket_balance"])
        fixed_lesson = self._create_fixed_lesson(lesson_date=lesson_date, title="二重返却防止テスト")
        mocked_now = timezone.make_aware(datetime(2026, 8, 6, 12, 0))

        with patch("django.utils.timezone.now", return_value=mocked_now):
            self._post_lesson_calendar_reserve(
                user=self.member,
                fixed_lesson=fixed_lesson,
                lesson_date=lesson_date,
            )

        first_copy = Reservation.objects.get(user=self.member, fixed_lesson=fixed_lesson)
        stale_copy = Reservation.objects.get(pk=first_copy.pk)
        self.assertTrue(first_copy.cancel(created_by=self.member))
        self.assertFalse(stale_copy.cancel(created_by=self.member))

        self.member.refresh_from_db()
        self.assertEqual(self.member.ticket_balance, 10)
        self.assertEqual(
            TicketLedger.objects.filter(
                reservation=first_copy,
                reason=TicketLedger.REASON_CANCEL_REFUND,
            ).count(),
            1,
        )

    def test_contractor_coach_can_take_other_coach_lesson(self):
        fixed_lesson = self._create_fixed_lesson(
            coach=self.coach,
            title="他コーチ担当レッスン",
        )

        response = self._post_lesson_calendar_reserve(
            user=self.contractor,
            fixed_lesson=fixed_lesson,
        )

        self.assertEqual(response.status_code, 302)

        reservation = Reservation.objects.get(user=self.contractor, fixed_lesson=fixed_lesson)
        self.assertEqual(reservation.status, Reservation.STATUS_ACTIVE)
        self.assertEqual(reservation.coach_id, self.coach.pk)

    def test_contractor_coach_cannot_take_own_lesson(self):
        fixed_lesson = self._create_fixed_lesson(
            coach=self.contractor,
            title="自分担当レッスン",
        )

        response = self._post_lesson_calendar_reserve(
            user=self.contractor,
            fixed_lesson=fixed_lesson,
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            Reservation.objects.filter(
                user=self.contractor,
                fixed_lesson=fixed_lesson,
                status=Reservation.STATUS_ACTIVE,
            ).exists()
        )

    def test_full_lesson_can_accept_waitlist_from_member(self):
        fixed_lesson = self._create_fixed_lesson(title="満員テストレッスン")

        members = []
        for index in range(6):
            members.append(
                self._create_user(
                    username=f"full_member_{index}",
                    role=self.User.ROLE_MEMBER,
                    full_name=f"満員 会員{index}",
                    ticket_balance=0,
                )
            )

        fixed_lesson.members.set(members)
        fixed_lesson.sync_future_reservations(created_by=self.coach)

        response = self._post_lesson_calendar_reserve(
            user=self.member,
            fixed_lesson=fixed_lesson,
            action="join_waitlist",
        )

        self.assertEqual(response.status_code, 302)
        waitlist = LessonWaitlist.objects.filter(
                fixed_lesson=fixed_lesson,
                user=self.member,
                status=LessonWaitlist.STATUS_WAITING,
            ).get()
        snapshot = LessonWaitlistParticipant.objects.get(waitlist=waitlist)
        self.assertEqual(snapshot.parent, self.member)
        self.assertEqual(snapshot.participant_type, "self")
        self.assertEqual(snapshot.participant_name, self.member.display_name())

        second_response = self._post_lesson_calendar_reserve(
            user=self.member,
            fixed_lesson=fixed_lesson,
            action="join_waitlist",
        )
        self.assertEqual(second_response.status_code, 302)
        self.assertEqual(
            LessonWaitlist.objects.filter(
                fixed_lesson=fixed_lesson,
                user=self.member,
                status=LessonWaitlist.STATUS_WAITING,
            ).count(),
            1,
        )

    def test_lesson_calendar_get_does_not_resync_or_mutate_fixed_lessons(self):
        self._create_fixed_lesson(title="閲覧時同期禁止テスト")
        with patch.object(FixedLesson, "sync_future_reservations") as sync_mock:
            response = self.client.get(reverse("club:lesson_calendar"))

        self.assertEqual(response.status_code, 200)
        sync_mock.assert_not_called()

    def test_waitlist_promote_rejects_protocol_relative_next_url(self):
        fixed_lesson = self._create_fixed_lesson(title="安全な戻り先テスト")
        start_at, end_at = fixed_lesson._build_datetimes_for_date(self.lesson_date)
        waitlist = LessonWaitlist.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court,
            fixed_lesson=fixed_lesson,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=end_at,
            status=LessonWaitlist.STATUS_CANCELED,
        )
        self.client.force_login(self.coach)

        response = self.client.post(
            reverse("club:lesson_waitlist_promote", args=[waitlist.pk]),
            data={"next": "//attacker.example/redirect"},
        )

        self.assertRedirects(
            response,
            reverse("club:reservation_list"),
            fetch_redirect_response=False,
        )

    def test_waitlist_promote_is_idempotent(self):
        self.member.ticket_balance = 10
        self.member.save(update_fields=["ticket_balance"])
        fixed_lesson = self._create_fixed_lesson(title="二重繰り上げ防止テスト")
        fixed_lesson.sync_future_reservations(created_by=self.coach)
        start_at, end_at = fixed_lesson._build_datetimes_for_date(self.lesson_date)
        waitlist = LessonWaitlist.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court,
            fixed_lesson=fixed_lesson,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=end_at,
        )
        self.client.force_login(self.coach)
        url = reverse("club:lesson_waitlist_promote", args=[waitlist.pk])

        first_response = self.client.post(url)
        second_response = self.client.post(url)

        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(second_response.status_code, 302)
        self.assertEqual(
            Reservation.objects.filter(
                user=self.member,
                fixed_lesson=fixed_lesson,
                status=Reservation.STATUS_ACTIVE,
            ).count(),
            1,
        )
        waitlist.refresh_from_db()
        self.assertEqual(waitlist.status, LessonWaitlist.STATUS_CONVERTED)

    def test_unassigned_coach_cannot_manage_another_lesson_court_expense(self):
        start_at = (timezone.now() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=start_at + timedelta(hours=2),
            capacity=6,
            status=CoachAvailability.STATUS_OPEN,
        )
        self.client.force_login(self.contractor)

        response = self.client.get(
            reverse("club:coach_expense_manage"),
            data={"availability_id": availability.pk},
        )

        self.assertEqual(response.status_code, 403)

    def test_court_expense_payer_must_be_one_of_the_main_coaches(self):
        unrelated_coach = self._create_user(
            username="unrelated_coach",
            role=self.User.ROLE_COACH,
            full_name="無関係 コーチ",
        )
        start_at = (timezone.now() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=start_at + timedelta(hours=2),
            capacity=6,
        )
        self.client.force_login(self.coach)

        response = self.client.post(
            reverse("club:coach_expense_manage"),
            data={
                "action": "create_court_transfer",
                "availability_id": availability.pk,
                "payer_coach_id": unrelated_coach.pk,
                "amount": "3000",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(CoachExpense.objects.exists())

    def test_court_expense_can_credit_a_different_main_coach(self):
        payer = self._create_user(
            username="main_payer",
            role=self.User.ROLE_COACH,
            full_name="清水峻平",
        )
        lesson_date = timezone.localdate() + timedelta(days=2)
        fixed_lesson = self._create_fixed_lesson(lesson_date=lesson_date, title="支払者振替テスト")
        fixed_lesson.sync_future_reservations(created_by=self.coach)
        start_at, _end_at = fixed_lesson._build_datetimes_for_date(lesson_date)
        availability = CoachAvailability.objects.get(
            coach=self.coach,
            court=self.court,
            start_at=start_at,
        )
        self.client.force_login(self.coach)

        calendar_response = self.client.get(
            reverse("club:lesson_calendar"),
            data={"year": start_at.year, "month": start_at.month},
        )
        expense_url = (
            f"{reverse('club:coach_expense_manage')}?"
            f"availability_id={availability.pk}&amp;date={start_at.date().isoformat()}"
        )
        self.assertContains(calendar_response, expense_url)

        response = self.client.post(
            reverse("club:coach_expense_manage"),
            data={
                "action": "create_court_transfer",
                "availability_id": availability.pk,
                "payer_coach_id": payer.pk,
                "amount": "3000",
            },
        )

        self.assertEqual(response.status_code, 302)
        expense = CoachExpense.objects.get()
        self.assertEqual(expense.created_by, payer)
        self.assertGreater(len(expense.note), 255)

        repeated_response = self.client.post(
            reverse("club:coach_expense_manage"),
            data={
                "action": "create_court_transfer",
                "availability_id": availability.pk,
                "payer_coach_id": payer.pk,
                "amount": "3000",
            },
        )
        self.assertEqual(repeated_response.status_code, 302)
        self.assertEqual(CoachExpense.objects.count(), 1)

        from .court_expense_transfer import _parse_note
        from .settlement_balance_policy import _court_transfer_allocation

        allocation = _court_transfer_allocation(
            [{"expense": expense, "amount": expense.amount, "meta": _parse_note(expense.note)}],
            [self.coach.pk, payer.pk],
        )
        self.assertEqual(allocation["burden_by_coach"], {self.coach.pk: 3000})
        self.assertEqual(allocation["reimbursement_by_coach"], {payer.pk: 3000})

    def test_direct_reservation_rejects_fixed_lesson_at_capacity(self):
        fixed_lesson = self._create_fixed_lesson(title="直接保存満員テスト")
        members = [
            self._create_user(
                username=f"capacity_member_{index}",
                role=self.User.ROLE_MEMBER,
                full_name=f"定員 会員{index}",
            )
            for index in range(6)
        ]
        fixed_lesson.members.set(members)
        start_at, end_at = fixed_lesson._build_datetimes_for_date(
            self.lesson_date,
        )
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=end_at,
            capacity=6,
            status=CoachAvailability.STATUS_OPEN,
        )

        with self.assertRaisesMessage(ValidationError, "このレッスンは満員です"):
            Reservation.objects.create(
                user=self.member,
                coach=self.coach,
                court=self.court,
                availability=availability,
                fixed_lesson=fixed_lesson,
                lesson_type=Reservation.LESSON_GENERAL,
                target_level=self.User.LEVEL_BEGINNER,
                start_at=start_at,
                end_at=end_at,
                status=Reservation.STATUS_ACTIVE,
            )

    def test_court_number_notice_uses_selected_calendar_lesson_without_dropdown(self):
        lesson_date = timezone.localdate() + timedelta(days=1)
        fixed_lesson = self._create_fixed_lesson(
            lesson_date=lesson_date,
            title="選択対象レッスン",
        )
        start_at, end_at = fixed_lesson._build_datetimes_for_date(lesson_date)
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=end_at,
            capacity=6,
            status=CoachAvailability.STATUS_OPEN,
        )
        first_member = self._create_user(
            username="line_first",
            role=self.User.ROLE_MEMBER,
            full_name="後藤 会員",
        )
        selected_member = self._create_user(
            username="line_selected",
            role=self.User.ROLE_MEMBER,
            full_name="阿部 会員",
        )
        other_lesson_member = self._create_user(
            username="line_other_lesson",
            role=self.User.ROLE_MEMBER,
            full_name="別枠 会員",
        )
        other_fixed_lesson = self._create_fixed_lesson(
            lesson_date=lesson_date,
            title="同時刻の別レッスン",
        )

        def create_reservation(user, target_fixed_lesson):
            return Reservation.objects.create(
                user=user,
                coach=self.coach,
                court=self.court,
                availability=availability,
                fixed_lesson=target_fixed_lesson,
                lesson_type=Reservation.LESSON_GENERAL,
                target_level=self.User.LEVEL_BEGINNER,
                start_at=start_at,
                end_at=end_at,
                status=Reservation.STATUS_ACTIVE,
            )

        create_reservation(first_member, fixed_lesson)
        selected_reservation = create_reservation(selected_member, fixed_lesson)
        create_reservation(other_lesson_member, other_fixed_lesson)

        self.client.force_login(self.coach)
        response = self.client.get(
            reverse("club:court_number_line_notice"),
            data={"slot_id": selected_reservation.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_slot"].pk, selected_reservation.pk)
        self.assertContains(
            response,
            f'name="slot_id" value="{selected_reservation.pk}"',
        )
        self.assertNotContains(response, '<select id="slot_id"')
        self.assertContains(response, "後藤 会員")
        self.assertContains(response, "阿部 会員")
        self.assertNotContains(response, "別枠 会員")

        cache.clear()
        send_data = {
            "slot_id": selected_reservation.pk,
            "court_number": "3コート",
            "note": "テスト連絡",
            "confirm_send": "yes",
            "action": "send",
        }
        with patch(
            "club.court_number_line_notice.notify_user_line_only",
            side_effect=[
                {"line": False, "email": False},
                {"line": True, "email": False},
            ],
        ) as line_notify_mock:
            with patch(
                "club.court_number_line_notice.notify_user_email_only",
                return_value={"line": False, "email": True},
            ) as email_notify_mock:
                first_send = self.client.post(
                    reverse("club:court_number_line_notice"),
                    data=send_data,
                )
                duplicate_send = self.client.post(
                    reverse("club:court_number_line_notice"),
                    data=send_data,
                )

        self.assertEqual(first_send.status_code, 302)
        self.assertEqual(duplicate_send.status_code, 302)
        self.assertEqual(line_notify_mock.call_count, 2)
        self.assertEqual(email_notify_mock.call_count, 1)
