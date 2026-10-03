from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.expense_metadata import build_expense_note, parse_expense_note
from club.models import (
    MAIN_COACH_NAMES,
    CoachAvailability,
    CoachExpense,
    Court,
    RainRefund,
    Reservation,
)


class CourtExpenseTransferZeroTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.admin = user_model.objects.create_user(
            username="court-zero-admin",
            full_name=MAIN_COACH_NAMES[0],
            role=user_model.ROLE_COACH,
            is_staff=True,
        )
        self.payer = user_model.objects.create_user(
            username="court-zero-payer",
            full_name=MAIN_COACH_NAMES[1],
            role=user_model.ROLE_COACH,
        )
        self.court = Court.objects.create(name="Zero court", is_active=True)
        lesson_date = timezone.localdate() + timedelta(days=3)
        start_at = timezone.make_aware(datetime.combine(lesson_date, time(17)))
        self.availability = CoachAvailability.objects.create(
            coach=self.admin,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=user_model.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=start_at + timedelta(hours=2),
            capacity=5,
        )
        self.url = reverse("club:coach_expense_manage")
        self.client.force_login(self.admin)

    @patch("club.settlement_service.recalculate_monthly_settlement_chain")
    def test_zero_is_saved_as_canonical_not_required_without_payer(self, recalculate):
        response = self.client.post(
            self.url,
            {
                "action": "create_court_transfer",
                "availability_id": self.availability.pk,
                "amount": "0",
            },
        )

        self.assertEqual(response.status_code, 302)
        expense = CoachExpense.objects.get()
        meta = parse_expense_note(expense.note)
        self.assertEqual(expense.amount, 0)
        self.assertEqual(expense.created_by_id, self.admin.pk)
        self.assertTrue(meta["court_cost_not_required"])
        self.assertIsNone(meta["payer_coach_id"])
        self.assertEqual(meta["payer_coach_name"], "登録不要")
        recalculate.assert_called_once()

    @patch("club.settlement_service.recalculate_monthly_settlement_chain")
    def test_existing_positive_transfer_can_be_corrected_and_refund_voided(self, _):
        transfer = CoachExpense.objects.create(
            expense_date=self.availability.start_at.date(),
            category=CoachExpense.CATEGORY_COURT,
            amount=2400,
            created_by=self.payer,
            note=build_expense_note(
                {
                    "expense_type": "court_transfer",
                    "approval_status": "approved",
                    "record_kind": "court_transfer",
                    "availability_id": self.availability.pk,
                    "payer_coach_id": self.payer.pk,
                }
            ),
        )
        refund_expense = CoachExpense.objects.create(
            expense_date=self.availability.start_at.date(),
            category=CoachExpense.CATEGORY_COURT,
            amount=2400,
            created_by=self.payer,
            note=build_expense_note(
                {
                    "expense_type": "court_transfer",
                    "approval_status": "refunded",
                    "record_kind": "cancellation_court_settlement",
                    "availability_id": self.availability.pk,
                }
            ),
        )
        refund = RainRefund.objects.create(
            expense=refund_expense,
            availability=self.availability,
            lesson_date=self.availability.start_at.date(),
            lesson_label="General rain cancellation",
            amount=2400,
            status=RainRefund.STATUS_REFUNDED,
            booking_account_kind=RainRefund.ACCOUNT_COACH,
            booking_account_coach=self.admin,
            debit_coach=self.admin,
            payer_coach=self.payer,
        )

        response = self.client.post(
            self.url,
            {
                "action": "create_court_transfer",
                "availability_id": self.availability.pk,
                "amount": "0",
                "confirm_refund_void": "1",
                "void_reason": "同時間帯のprivateでコート使用",
            },
        )

        self.assertEqual(response.status_code, 302)
        transfer.refresh_from_db()
        refund.refresh_from_db()
        self.assertEqual(CoachExpense.objects.filter(pk=transfer.pk).count(), 1)
        self.assertEqual(transfer.amount, 0)
        self.assertTrue(parse_expense_note(transfer.note)["court_cost_not_required"])
        self.assertEqual(refund.status, RainRefund.STATUS_VOIDED)

    def test_positive_amount_still_requires_payer(self):
        response = self.client.post(
            self.url,
            {
                "action": "create_court_transfer",
                "availability_id": self.availability.pk,
                "amount": "1400",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(CoachExpense.objects.exists())
        self.assertContains(response, "支払ったメインコーチを選択")

    @patch("club.settlement_service.recalculate_monthly_settlement_chain")
    def test_company_wallet_payer_is_saved_without_fake_coach(self, recalculate):
        response = self.client.post(
            self.url,
            {
                "action": "create_court_transfer",
                "availability_id": self.availability.pk,
                "amount": "2400",
                "payer_coach_id": "company_wallet",
            },
        )

        self.assertEqual(response.status_code, 302)
        expense = CoachExpense.objects.get()
        meta = parse_expense_note(expense.note)
        self.assertEqual(expense.created_by_id, self.admin.pk)
        self.assertEqual(meta["payer_kind"], "company_wallet")
        self.assertIsNone(meta["payer_coach_id"])
        self.assertEqual(meta["payer_coach_name"], "会社の財布")
        self.assertEqual(meta["recorded_by_id"], self.admin.pk)
        recalculate.assert_called_once()
