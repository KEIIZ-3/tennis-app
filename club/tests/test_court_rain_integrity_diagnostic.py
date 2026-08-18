import json
from datetime import datetime, time, timedelta

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from club.court_rain_integrity_diagnostic import diagnose_court_rain_integrity
from club.expense_metadata import build_expense_note
from club.models import CoachAvailability, CoachExpense, Court, RainRefund, Reservation, User


class CourtRainIntegrityDiagnosticTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(
            username="diagnostic-coach", password="unused", role=User.ROLE_COACH,
            email="secret@example.com", full_name="Private Person",
        )
        self.coach.phone_number = "090-0000-0000"
        self.court = Court.objects.create(name="Diagnostic court", is_active=True)
        start = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=5), time(10))
        )
        self.availability = CoachAvailability.objects.create(
            coach=self.coach, court=self.court, lesson_type=Reservation.LESSON_PRIVATE,
            target_level=User.LEVEL_BEGINNER, start_at=start,
            end_at=start + timedelta(hours=1), capacity=1,
        )

    def _expense(self, *, amount=2400, status="approved", availability_id=None,
                 record_kind="court_transfer", category=CoachExpense.CATEGORY_COURT):
        return CoachExpense.objects.create(
            expense_date=timezone.localdate(), category=category, amount=amount,
            created_by=self.coach,
            note=build_expense_note({
                "expense_type": "court_transfer", "record_kind": record_kind,
                "availability_id": availability_id or self.availability.pk,
                "approval_status": status, "using_coach_ids": [self.coach.pk],
                "token": "must-not-leak",
            }),
        )

    def _refund(self, expense, *, status=RainRefund.STATUS_PENDING, availability=None):
        return RainRefund.objects.create(
            expense=expense, availability=availability or self.availability,
            lesson_date=timezone.localdate(), amount=expense.amount, status=status,
            booking_account_kind=RainRefund.ACCOUNT_COACH,
            booking_account_coach=self.coach, debit_coach=self.coach,
            payer_coach=self.coach,
        )

    def _snapshot(self):
        return (
            list(CoachExpense.objects.order_by("pk").values()),
            list(RainRefund.objects.order_by("pk").values()),
        )

    def test_normal_transfer_and_refund_have_no_findings_and_do_not_write(self):
        expense = self._expense(status="refund_pending")
        self._refund(expense)
        before = self._snapshot()
        result = diagnose_court_rain_integrity()
        self.assertEqual(result["finding_count"], 0)
        self.assertEqual(self._snapshot(), before)

    def test_cancellation_settlements_for_august_refunds_have_no_findings(self):
        for day in (6, 10, 25):
            with self.subTest(day=day):
                availability = CoachAvailability.objects.create(
                    coach=self.coach, court=self.court,
                    lesson_type=Reservation.LESSON_PRIVATE,
                    target_level=User.LEVEL_BEGINNER,
                    start_at=timezone.make_aware(datetime(2026, 8, day, 19)),
                    end_at=timezone.make_aware(datetime(2026, 8, day, 21)),
                    capacity=1,
                )
                expense = self._expense(
                    status="refund_pending",
                    availability_id=availability.pk,
                    record_kind="cancellation_court_settlement",
                )
                self._refund(expense, availability=availability)

        result = diagnose_court_rain_integrity()
        self.assertEqual(result["finding_count"], 0)

    def test_cancellation_settlements_are_not_normal_transfer_duplicates(self):
        first = self._expense(record_kind="cancellation_court_settlement")
        self._expense(record_kind="cancellation_court_settlement")
        self._refund(first)

        result = diagnose_court_rain_integrity()

        self.assertEqual(result["duplicate_court_transfers"], [])
        self.assertEqual(result["finding_count"], 0)

    def test_duplicate_transfers_report_unified_selection_and_safe_ids(self):
        first = self._expense(amount=1000)
        second = self._expense(amount=2000)
        result = diagnose_court_rain_integrity()
        row = result["duplicate_court_transfers"][0]
        self.assertEqual(row["expense_ids"], [first.pk, second.pk])
        self.assertEqual(row["registration_selected_pk"], second.pk)
        self.assertEqual(row["settlement_selected_pk"], second.pk)
        self.assertTrue(row["selection_matches"])
        output = json.dumps(result)
        for secret in ("secret@example.com", "090-0000-0000", "Private Person", "must-not-leak"):
            self.assertNotIn(secret, output)

    def test_duplicate_refunds_are_reported(self):
        first = self._expense(status="refund_pending")
        second = self._expense(status="refund_pending")
        self._refund(first)
        self._refund(second)
        self.assertEqual(
            diagnose_court_rain_integrity()["duplicate_rain_refunds"][0]["count"], 2
        )

    def test_both_state_mismatch_directions_are_reported(self):
        pending = self._expense(status="refunded")
        refunded = self._expense(status="refund_pending")
        self._refund(pending, status=RainRefund.STATUS_PENDING)
        self._refund(refunded, status=RainRefund.STATUS_REFUNDED)
        rows = diagnose_court_rain_integrity()["state_mismatches"]
        self.assertEqual(len(rows), 2)

    def test_metadata_availability_mismatch_and_invalid_transfer_are_reported(self):
        other = CoachAvailability.objects.create(
            coach=self.coach, court=self.court, lesson_type=Reservation.LESSON_PRIVATE,
            target_level=User.LEVEL_BEGINNER,
            start_at=self.availability.start_at + timedelta(hours=2),
            end_at=self.availability.end_at + timedelta(hours=2), capacity=1,
        )
        mismatched = self._expense(status="refund_pending")
        invalid = self._expense(status="refund_pending", record_kind="other")
        self._refund(mismatched, availability=other)
        self._refund(invalid)
        rows = diagnose_court_rain_integrity()["metadata_mismatches"]
        self.assertEqual(len(rows), 2)
        self.assertIn("availability_id_mismatch", rows[0]["reasons"])
        self.assertIn("expense_is_not_court_transfer", rows[1]["reasons"])

    def test_refund_state_without_rain_refund_is_reported(self):
        expense = self._expense(status="refunded")
        row = diagnose_court_rain_integrity()["missing_rain_refunds"][0]
        self.assertEqual(row["expense_id"], expense.pk)

    def test_command_succeeds_with_findings_and_does_not_write(self):
        self._expense(); self._expense()
        before = self._snapshot()
        from io import StringIO
        stdout = StringIO()
        call_command("diagnose_court_rain_integrity", stdout=stdout)
        result = json.loads(stdout.getvalue())
        self.assertGreater(result["finding_count"], 0)
        self.assertEqual(self._snapshot(), before)
        for secret in ("secret@example.com", "must-not-leak"):
            self.assertNotIn(secret, stdout.getvalue())

    def test_command_succeeds_without_findings(self):
        expense = self._expense(status="refund_pending")
        self._refund(expense)
        from io import StringIO
        stdout = StringIO()
        call_command("diagnose_court_rain_integrity", stdout=stdout)
        self.assertEqual(json.loads(stdout.getvalue())["finding_count"], 0)
