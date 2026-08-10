from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from club.expense_metadata import build_expense_note, parse_expense_note
from club.models import CoachAvailability, CoachExpense, Court, RainRefund, Reservation
from club.rain_refund_service import confirm_rain_refund


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
