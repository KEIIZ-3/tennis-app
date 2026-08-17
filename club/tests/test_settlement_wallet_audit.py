from datetime import datetime, timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from club.expense_metadata import build_expense_note
from club.models import CoachAvailability, CoachExpense, Court, Reservation, User
from club.settlement_wallet_audit import _court_cost_audit_rows


class SettlementWalletCourtAuditTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(
            username="wallet-court-audit-coach",
            full_name="Audit Coach",
            role=User.ROLE_COACH,
        )
        self.court = Court.objects.create(name="Audit Court", is_active=True)

    def availability(self, hour, *, court=None, court_count=1):
        start = timezone.make_aware(datetime(2026, 8, 5, hour))
        return CoachAvailability.objects.create(
            coach=self.coach,
            court=court or self.court,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=User.LEVEL_BEGINNER,
            start_at=start,
            end_at=start + timedelta(hours=2),
            capacity=1,
            court_count=court_count,
        )

    def expense(self, availability, amount):
        return CoachExpense.objects.create(
            expense_date=availability.start_at.date(),
            category=CoachExpense.CATEGORY_COURT,
            amount=amount,
            created_by=self.coach,
            note=build_expense_note({
                "expense_type": "court_transfer",
                "approval_status": "approved",
                "record_kind": "court_transfer",
                "availability_id": availability.pk,
                "payer_coach_id": self.coach.pk,
                "using_coach_ids": [self.coach.pk],
            }),
        )

    def test_same_date_different_occurrences_are_both_included(self):
        first = self.expense(self.availability(17), 2600)
        second = self.expense(self.availability(19), 2600)
        policy = {"detail_rows": [
            {"expense_id": first.pk, "execution_status": "held"},
            {"expense_id": second.pk, "execution_status": "held"},
        ]}

        rows = _court_cost_audit_rows(2026, 8, policy)

        self.assertEqual([row["included"] for row in rows], [True, True])
        self.assertNotEqual(
            rows[0]["canonical_occurrence_key"],
            rows[1]["canonical_occurrence_key"],
        )
        self.assertEqual(
            [row["start_at"][11:16] for row in rows],
            ["17:00", "19:00"],
        )

    def test_same_occurrence_marks_latest_canonical_and_old_row_excluded(self):
        availability = self.availability(17)
        old = self.expense(availability, 2400)
        latest = self.expense(availability, 2600)
        policy = {"detail_rows": [
            {"expense_id": latest.pk, "execution_status": "held"},
        ]}

        rows = _court_cost_audit_rows(2026, 8, policy)
        by_id = {row["expense_id"]: row for row in rows}

        self.assertFalse(by_id[old.pk]["included"])
        self.assertFalse(by_id[old.pk]["is_canonical"])
        self.assertEqual(by_id[old.pk]["duplicate_of"], latest.pk)
        self.assertIn(f"expense_id {latest.pk}", by_id[old.pk]["included_reason"])
        self.assertTrue(by_id[latest.pk]["included"])
        self.assertTrue(by_id[latest.pk]["is_canonical"])
        self.assertEqual(sum(row["canonical_cost"] for row in rows), 2600)

    def test_two_courts_and_multiple_coaches_do_not_multiply_registered_cost(self):
        availability = self.availability(19, court_count=2)
        CoachAvailability.objects.filter(pk=availability.pk).update(court_count=2)
        availability.refresh_from_db()
        expense = self.expense(availability, 5200)
        policy = {"detail_rows": [
            {"expense_id": expense.pk, "execution_status": "held"},
        ]}

        row = _court_cost_audit_rows(2026, 8, policy)[0]

        self.assertEqual(row["court_count"], 2)
        self.assertEqual(row["registered_cost"], 5200)
        self.assertEqual(row["canonical_cost"], 5200)
        self.assertEqual(row["using_coach_ids"], [self.coach.pk])

    def test_canonical_but_canceled_row_is_explained_as_excluded(self):
        expense = self.expense(self.availability(19), 2600)

        row = _court_cost_audit_rows(2026, 8, {"detail_rows": []})[0]

        self.assertTrue(row["is_canonical"])
        self.assertFalse(row["included"])
        self.assertEqual(row["canonical_cost"], 0)
        self.assertIn("cancellation", row["included_reason"])

    def test_court_audit_is_select_only(self):
        expense = self.expense(self.availability(19), 2600)
        policy = {"detail_rows": [{"expense_id": expense.pk}]}

        with CaptureQueriesContext(connection) as queries:
            _court_cost_audit_rows(2026, 8, policy)

        self.assertTrue(queries.captured_queries)
        self.assertTrue(all(
            query["sql"].lstrip().upper().startswith("SELECT")
            for query in queries.captured_queries
        ))
