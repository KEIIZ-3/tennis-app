from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.expense_metadata import build_expense_note
from club.models import CoachAvailability, CoachExpense, Court, Reservation, User


class AvailabilityCourtAccountingTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="court-admin", password="unused", role=User.ROLE_COACH,
            is_staff=True,
        )
        self.coach = User.objects.create_user(
            username="court-payer", password="unused", role=User.ROLE_COACH,
            full_name="井上 春佳",
        )
        start = timezone.make_aware(datetime.combine(
            timezone.localdate() + timedelta(days=7), time(10),
        ))
        self.availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=Court.objects.create(name="事前登録テストコート", is_active=True),
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=User.LEVEL_BEGINNER,
            start_at=start,
            end_at=start + timedelta(hours=1),
            capacity=1,
        )
        self.client.force_login(self.admin)

    def test_optional_company_and_coach_defaults_are_valid(self):
        self.availability.full_clean()
        self.availability.court_payer_kind = "company_wallet"
        self.availability.full_clean()
        self.availability.court_payer_kind = "coach"
        self.availability.court_payer_coach = self.coach
        self.availability.court_booking_account_kind = "coach"
        self.availability.court_booking_account_coach = self.coach
        self.availability.full_clean()

    def test_booking_other_and_invalid_combinations(self):
        self.availability.court_booking_account_kind = "other"
        self.availability.court_booking_account_other = "共通予約アカウント"
        self.availability.full_clean()
        self.availability.court_booking_account_coach = self.coach
        with self.assertRaises(ValidationError):
            self.availability.full_clean()

    def test_new_transfer_uses_advance_payer_but_existing_actual_wins(self):
        self.availability.court_payer_kind = "company_wallet"
        self.availability.save(update_fields=["court_payer_kind"])
        response = self.client.get(reverse("club:coach_expense_manage"), {
            "availability_id": self.availability.pk,
        })
        self.assertEqual(response.context["existing_payer_id"], "company_wallet")

        CoachExpense.objects.create(
            expense_date=timezone.localdate(), category=CoachExpense.CATEGORY_COURT,
            amount=2000, created_by=self.admin,
            note=build_expense_note({
                "record_kind": "court_transfer",
                "availability_id": self.availability.pk,
                "approval_status": "approved",
                "payer_kind": "coach",
                "payer_coach_id": self.coach.pk,
            }),
        )
        response = self.client.get(reverse("club:coach_expense_manage"), {
            "availability_id": self.availability.pk,
        })
        self.assertEqual(response.context["existing_payer_id"], str(self.coach.pk))

    @patch("club.settlement_service.recalculate_monthly_settlement_chain")
    def test_transfer_save_synchronizes_actual_payer(self, recalculate):
        response = self.client.post(reverse("club:coach_expense_manage"), {
            "action": "create_court_transfer",
            "availability_id": self.availability.pk,
            "payer_coach_id": "company_wallet",
            "amount": "2400",
        })
        self.assertEqual(response.status_code, 302)
        self.availability.refresh_from_db()
        self.assertEqual(self.availability.court_payer_kind, "company_wallet")
        self.assertIsNone(self.availability.court_payer_coach)
        self.assertEqual(CoachExpense.objects.count(), 1)
