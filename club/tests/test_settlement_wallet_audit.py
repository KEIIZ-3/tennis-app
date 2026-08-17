from datetime import date, datetime, timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from club.expense_metadata import build_expense_note
from club.models import (
    CoachAvailability, CoachExpense, Court, FixedLesson, Reservation,
    TicketConsumption, TicketPurchase, User,
)
from club.settlement_models import MonthlySettlement
from club.lesson_execution_storage import save_status
from club.settlement_wallet_audit import _court_cost_audit_rows, audit_wallet_month


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

    def test_replacement_availabilities_for_fixed_occurrence_share_canonical_expense(self):
        fixed = FixedLesson.objects.create(
            title="Replacement court lesson",
            coach=self.coach,
            court=self.court,
            start_date=date(2026, 8, 5),
            weekday=2,
            start_hour=19,
        )
        old_availability = self.availability(19)
        replacement = self.availability(19)
        member = User.objects.create_user(username="replacement-court-member")
        Reservation.objects.bulk_create([
            Reservation(
                user=member,
                coach=self.coach,
                court=availability.court,
                availability=availability,
                fixed_lesson=fixed,
                start_at=availability.start_at,
                end_at=availability.end_at,
            )
            for availability in (old_availability, replacement)
        ])
        old = self.expense(old_availability, 2600)
        latest = self.expense(replacement, 2600)
        policy = {"detail_rows": [{"expense_id": latest.pk}]}

        rows = _court_cost_audit_rows(2026, 8, policy)
        by_id = {row["expense_id"]: row for row in rows}

        self.assertEqual(
            by_id[old.pk]["canonical_occurrence_key"],
            by_id[latest.pk]["canonical_occurrence_key"],
        )
        self.assertFalse(by_id[old.pk]["included"])
        self.assertEqual(by_id[old.pk]["duplicate_of"], latest.pk)
        self.assertTrue(by_id[latest.pk]["included"])

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


class SettlementWalletTicketRevenueAuditTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user(username="wallet-ticket-member")
        self.coach = User.objects.create_user(
            username="wallet-ticket-coach", role=User.ROLE_COACH
        )
        self.court = Court.objects.create(name="Wallet ticket court")
        self.settlement = MonthlySettlement.objects.create(year=2026, month=8)

    def create_consumption(self, day, *, status=Reservation.STATUS_ACTIVE,
                           unit_price=3500, refunded=False, purchase_type="set4"):
        start_at = timezone.make_aware(datetime(2026, 8, day, 10))
        availability = CoachAvailability.objects.create(
            coach=self.coach, court=self.court, start_at=start_at,
            end_at=start_at + timedelta(hours=2), capacity=4,
        )
        reservation = Reservation.objects.create(
            user=self.member, coach=self.coach, court=self.court,
            availability=availability, start_at=start_at,
            end_at=start_at + timedelta(hours=2), status=status, tickets_used=1,
        )
        purchase = TicketPurchase.objects.create(
            user=self.member, purchase_type=purchase_type, total_tickets=4,
            remaining_tickets=3, unit_price=unit_price, purchased_at=start_at,
        )
        consumption = TicketConsumption.objects.create(
            user=self.member, purchase=purchase, reservation=reservation,
            tickets_used=1, unit_price_snapshot=unit_price,
            refunded_at=timezone.now() if refunded else None,
        )
        return reservation, consumption

    def test_only_held_past_consumption_is_recognized_as_revenue(self):
        held, _held_consumption = self.create_consumption(2)
        future, _future_consumption = self.create_consumption(28)
        canceled, _canceled_consumption = self.create_consumption(
            3, status=Reservation.STATUS_CANCELED
        )
        rain, _rain_consumption = self.create_consumption(
            4, status=Reservation.STATUS_RAIN_CANCELED
        )
        refunded, _refunded_consumption = self.create_consumption(5, refunded=True)
        _zero, _zero_consumption = self.create_consumption(
            6, unit_price=0, purchase_type=TicketPurchase.PURCHASE_TYPE_ADMIN
        )
        save_status(self.settlement, f"availability:{held.availability_id}", "held", self.coach)
        save_status(self.settlement, f"availability:{refunded.availability_id}", "held", self.coach)

        with patch("club.settlement_wallet_audit.timezone.now", return_value=timezone.make_aware(datetime(2026, 8, 17, 12))):
            result = audit_wallet_month(2026, 8)

        self.assertEqual(result["ticket_consumption_revenue_total"], 3500)
        rows = {row["reservation_id"]: row for row in result["ticket_consumption_rows"]}
        self.assertTrue(rows[held.pk]["included"])
        self.assertFalse(rows[future.pk]["included"])
        self.assertFalse(rows[canceled.pk]["included"])
        self.assertFalse(rows[rain.pk]["included"])
        self.assertFalse(rows[refunded.pk]["included"])
        self.assertEqual(rows[future.pk]["included_reason"], "future occurrence; inventory only")
        self.assertEqual(rows[held.pk]["purchase_amount"], 14000)
