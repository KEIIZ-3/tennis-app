from datetime import date, datetime, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.expense_metadata import build_expense_note, parse_expense_note
from club.models import MAIN_COACH_NAMES, CoachAvailability, CoachExpense, Court, RainRefund, Reservation
from club.rain_refund_service import confirm_rain_refund
from club.settlement_models import MonthlySettlement


class RainRefundServiceTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.coach = user_model.objects.create_user(
            username="rain_refund_coach",
            password="test-password",
            role=user_model.ROLE_COACH,
        )
        self.court = Court.objects.create(name="Rain test court", is_active=True)
        lesson_date = timezone.localdate() + timedelta(days=2)
        start_at = timezone.make_aware(datetime.combine(lesson_date, time(10)))
        self.availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=user_model.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=start_at + timedelta(hours=1),
            capacity=1,
        )
        self.expense = CoachExpense.objects.create(
            expense_date=timezone.localdate(),
            category=CoachExpense.CATEGORY_COURT,
            amount=2400,
            created_by=self.coach,
            note=build_expense_note(
                {
                    "expense_type": "court_transfer",
                    "approval_status": "refund_pending",
                    "record_kind": "court_transfer",
                    "availability_id": self.availability.pk,
                },
                "court fee",
            ),
        )
        self.refund = RainRefund.objects.create(
            expense=self.expense,
            availability=self.availability,
            lesson_date=timezone.localdate(),
            lesson_label="Rain test lesson",
            amount=2400,
            status=RainRefund.STATUS_PENDING,
            booking_account_kind=RainRefund.ACCOUNT_COACH,
            booking_account_coach=self.coach,
            debit_coach=self.coach,
            payer_coach=self.coach,
        )

    def test_confirmation_updates_both_persisted_representations(self):
        confirm_rain_refund(self.expense.pk, confirmed_by=self.coach)

        self.refund.refresh_from_db()
        self.expense.refresh_from_db()
        meta = parse_expense_note(self.expense.note)
        self.assertEqual(self.refund.status, RainRefund.STATUS_REFUNDED)
        self.assertIsNotNone(self.refund.confirmed_at)
        self.assertEqual(self.refund.confirmed_by, self.coach)
        self.assertEqual(meta["approval_status"], "refunded")
        self.assertEqual(meta["court_refunded_by_id"], self.coach.pk)
        self.assertEqual(meta["plain_note"], "court fee")

    def test_repeated_confirmation_is_idempotent(self):
        confirm_rain_refund(self.expense.pk, confirmed_by=self.coach)
        self.refund.refresh_from_db()
        first_confirmed_at = self.refund.confirmed_at

        confirm_rain_refund(self.expense.pk, confirmed_by=self.coach)

        self.refund.refresh_from_db()
        self.assertEqual(self.refund.confirmed_at, first_confirmed_at)
        self.assertEqual(RainRefund.objects.filter(expense=self.expense).count(), 1)

    def test_expense_write_failure_rolls_back_confirmation(self):
        with patch.object(CoachExpense, "save", side_effect=RuntimeError("write failed")):
            with self.assertRaisesMessage(RuntimeError, "write failed"):
                confirm_rain_refund(self.expense.pk, confirmed_by=self.coach)

        self.refund.refresh_from_db()
        self.expense.refresh_from_db()
        self.assertEqual(self.refund.status, RainRefund.STATUS_PENDING)
        self.assertEqual(
            parse_expense_note(self.expense.note)["approval_status"],
            "refund_pending",
        )

    def test_closed_month_rejects_confirmation(self):
        MonthlySettlement.objects.create(
            year=self.refund.lesson_date.year,
            month=self.refund.lesson_date.month,
            status=MonthlySettlement.STATUS_CLOSED,
        )

        with self.assertRaisesMessage(ValidationError, "締め済みの月"):
            confirm_rain_refund(self.expense.pk, confirmed_by=self.coach)

        self.refund.refresh_from_db()
        self.expense.refresh_from_db()
        self.assertEqual(self.refund.status, RainRefund.STATUS_PENDING)
        self.assertEqual(
            parse_expense_note(self.expense.note)["approval_status"],
            "refund_pending",
        )

    def test_status_other_than_pending_or_refunded_is_rejected(self):
        RainRefund.objects.filter(pk=self.refund.pk).update(status="invalid")

        with self.assertRaisesMessage(ValidationError, "返金待ち"):
            confirm_rain_refund(self.expense.pk, confirmed_by=self.coach)

        self.refund.refresh_from_db()
        self.assertEqual(self.refund.status, "invalid")


class RainRefundSettlementViewTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.admin = user_model.objects.create_user(
            username="rain-refund-admin",
            full_name=MAIN_COACH_NAMES[0],
            role=user_model.ROLE_COACH,
            is_staff=True,
        )
        self.debit_coach = user_model.objects.create_user(
            username="rain-refund-debit",
            full_name="清水峻平",
            role=user_model.ROLE_COACH,
        )
        self.payer_coach = user_model.objects.create_user(
            username="rain-refund-payer",
            full_name="井上春佳",
            role=user_model.ROLE_COACH,
        )
        self.expense = CoachExpense.objects.create(
            expense_date=date(2026, 7, 5),
            category=CoachExpense.CATEGORY_COURT,
            amount=2400,
            created_by=self.payer_coach,
            note=build_expense_note(
                {
                    "expense_type": "court_transfer",
                    "approval_status": "refund_pending",
                    "record_kind": "cancellation_court_settlement",
                },
                "雨天中止",
            ),
        )
        self.refund = RainRefund.objects.create(
            expense=self.expense,
            lesson_date=date(2026, 7, 5),
            lesson_label="雨天中止レッスン",
            amount=2400,
            status=RainRefund.STATUS_PENDING,
            booking_account_kind=RainRefund.ACCOUNT_COACH,
            booking_account_coach=self.debit_coach,
            debit_coach=self.debit_coach,
            payer_coach=self.payer_coach,
        )
        self.client.force_login(self.admin)

    def test_pending_refund_confirmation_recalculates_settlement_once(self):
        url = reverse("club:coach_admin_settlement")
        before = self.client.get(url, {"year": 2026, "month": 7})
        self.assertEqual(before.context["rain_refund_pending_total"], 2400)
        self.assertEqual(before.context["rain_refunded_total"], 0)

        response = self.client.post(
            url,
            {
                "action": "confirm_rain_refund",
                "expense_id": self.expense.pk,
                "year": 2026,
                "month": 7,
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["rain_refund_pending_total"], 0)
        self.assertEqual(response.context["rain_refunded_total"], 2400)
        rows = {
            row["coach_name"]: row for row in response.context["coach_rows"]
        }
        self.assertEqual(rows["清水峻平"]["rain_refund_burden"], 2400)
        self.assertEqual(rows["井上春佳"]["rain_refund_reimbursement"], 2400)

        first_confirmed_at = RainRefund.objects.get(pk=self.refund.pk).confirmed_at
        self.client.post(
            url,
            {
                "action": "confirm_rain_refund",
                "expense_id": self.expense.pk,
                "year": 2026,
                "month": 7,
            },
        )
        self.refund.refresh_from_db()
        self.assertEqual(self.refund.confirmed_at, first_confirmed_at)

    def test_non_admin_cannot_confirm_refund(self):
        self.client.force_login(self.debit_coach)

        response = self.client.post(
            reverse("club:coach_admin_settlement"),
            {
                "action": "confirm_rain_refund",
                "expense_id": self.expense.pk,
                "year": 2026,
                "month": 7,
            },
        )

        self.assertEqual(response.status_code, 403)
        self.refund.refresh_from_db()
        self.assertEqual(self.refund.status, RainRefund.STATUS_PENDING)
