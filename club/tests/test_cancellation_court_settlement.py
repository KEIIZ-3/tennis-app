from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import RequestFactory, TestCase
from django.utils import timezone

from club import lesson_execution
from club.court_fee_service import calculate_availability_court_fee
from club.expense_metadata import build_expense_note, parse_expense_note
from club.models import MAIN_COACH_NAMES, CoachAvailability, CoachExpense, Court, RainRefund, Reservation
from club.settlement_balance_policy import _court_transfer_allocation


class CancellationCourtSettlementTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.coaches = [
            User.objects.create_user(
                username=f"main-{index}", full_name=name, role=User.ROLE_COACH
            )
            for index, name in enumerate(MAIN_COACH_NAMES, start=1)
        ]
        self.court = Court.objects.create(
            name="中止精算テストコート",
            is_active=True,
            court_type=Court.COURT_SONO,
        )
        start_at = timezone.make_aware(datetime(2026, 8, 17, 19, 0))
        self.availability = CoachAvailability.objects.create(
            coach=self.coaches[0],
            court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=start_at + timedelta(hours=2),
            capacity=1,
            status=CoachAvailability.STATUS_OPEN,
        )

    def _refund_input(self):
        return {
            "account_kind": "coach",
            "account_coach": self.coaches[0],
            "account_other": "",
            "collection_coach": self.coaches[1],
            "payer_coach": self.coaches[2],
            "debit_coach": self.coaches[0],
        }

    def _create_cancellation(self):
        with transaction.atomic():
            return lesson_execution._mark_court_expense_refund_pending(
                self.availability,
                changed_by=self.coaches[0],
                refund_input=self._refund_input(),
            )

    def test_cancellation_without_normal_court_expense_uses_automatic_fee(self):
        expense = self._create_cancellation()
        refund = RainRefund.objects.get(availability=self.availability)

        self.assertEqual(
            expense.amount,
            calculate_availability_court_fee(self.availability)["total"],
        )
        self.assertEqual(refund.amount, expense.amount)
        self.assertEqual(refund.booking_account_coach, self.coaches[0])
        self.assertEqual(refund.collection_coach, self.coaches[1])
        self.assertEqual(refund.payer_coach, self.coaches[2])
        self.assertEqual(
            parse_expense_note(expense.note)["record_kind"],
            "cancellation_court_settlement",
        )

    def test_normal_expense_is_retained_and_excluded_after_cancellation(self):
        normal = CoachExpense.objects.create(
            expense_date=self.availability.start_at.date(),
            category=CoachExpense.CATEGORY_COURT,
            amount=2600,
            created_by=self.coaches[1],
            note=build_expense_note(
                {
                    "expense_type": "court_transfer",
                    "approval_status": "approved",
                    "record_kind": "court_transfer",
                    "availability_id": self.availability.pk,
                    "payer_coach_id": self.coaches[1].pk,
                    "using_coach_ids": [self.coaches[0].pk],
                },
                "通常実施用",
            ),
        )

        cancellation = self._create_cancellation()
        normal.refresh_from_db()
        allocation = _court_transfer_allocation(
            [{
                "expense": normal,
                "amount": normal.amount,
                "meta": parse_expense_note(normal.note),
            }],
            eligible_coach_ids=[coach.pk for coach in self.coaches],
            main_coach_ids=[coach.pk for coach in self.coaches],
            excluded_availability_ids={self.availability.pk},
        )

        self.assertNotEqual(normal.pk, cancellation.pk)
        self.assertEqual(parse_expense_note(normal.note)["approval_status"], "approved")
        self.assertEqual(allocation["burden_by_coach"], {})
        self.assertEqual(allocation["reimbursement_by_coach"], {})

    def test_duplicate_submission_reuses_cancellation_record(self):
        first = self._create_cancellation()
        second = self._create_cancellation()

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(RainRefund.objects.filter(availability=self.availability).count(), 1)

    def test_collector_is_required_independently_of_booking_account(self):
        request = RequestFactory().post(
            "/",
            {
                "rain_booking_account": str(self.coaches[0].pk),
                "rain_court_payer_id": str(self.coaches[2].pk),
            },
        )

        value, error = lesson_execution._rain_refund_input(request)

        self.assertIsNone(value)
        self.assertEqual(error, "回収予定コーチを選択してください。")
