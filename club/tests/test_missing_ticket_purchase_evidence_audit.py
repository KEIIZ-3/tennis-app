import json
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from club.models import Court, Reservation, TicketLedger, TicketPurchase, User


class MissingTicketPurchaseEvidenceAuditTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.user = User.objects.create_user(username="member", full_name="監査 会員", ticket_balance=-1)
        self.coach = User.objects.create_user(username="coach", role=User.ROLE_COACH)
        self.court = Court.objects.create(name="audit court")

    def reservation(self):
        row = Reservation(user=self.user, coach=self.coach, court=self.court,
            start_at=self.now - timedelta(days=1), end_at=self.now - timedelta(days=1, hours=-1),
            tickets_used=1, ticket_consumed_at=self.now - timedelta(days=1), participant_ticket_price_snapshot=3500)
        Reservation.objects.bulk_create([row])
        ledger = TicketLedger.objects.create(user=self.user, reservation=row, change_amount=-1,
            balance_after=-1, reason=TicketLedger.REASON_RESERVATION_USE)
        return row, ledger

    def test_command_reuses_repair_candidates_and_performs_selects_only(self):
        reservation, ledger = self.reservation()
        purchase = TicketPurchase.objects.create(user=self.user, total_tickets=2, remaining_tickets=1,
            unit_price=3500, purchased_at=self.now - timedelta(days=2), label="evidence", note="persisted")
        # auto_now_add cannot model historical import ordering without this persisted update.
        TicketLedger.objects.filter(pk=ledger.pk).update(created_at=self.now - timedelta(days=1))
        before = (self.user.ticket_balance, purchase.remaining_tickets, TicketLedger.objects.count())
        stdout = StringIO()
        with CaptureQueriesContext(connection) as queries:
            call_command("audit_missing_ticket_purchase_evidence", "--reservation-id", str(reservation.id), stdout=stdout)
        result = json.loads(stdout.getvalue())
        row = result["rows"][0]
        self.assertEqual(row["candidate_count"], 1)
        self.assertEqual(row["candidate_purchases"][0]["purchase_id"], purchase.id)
        self.assertEqual(row["repair_rejection_reason"], "persisted_evidence_consistent")
        self.assertFalse(row["linkage_possible"])
        self.assertEqual(row["block_reason"], "no_later_purchase_capacity")
        self.assertEqual(result["summary"]["missing_count"], 1)
        self.assertEqual(result["summary"]["negative_balance_user_count"], 1)
        self.assertTrue(result["user_timelines"][str(self.user.id)])
        self.assertTrue(all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries.captured_queries))
        self.user.refresh_from_db(); purchase.refresh_from_db()
        self.assertEqual((self.user.ticket_balance, purchase.remaining_tickets, TicketLedger.objects.count()), before)

    def test_classifies_multiple_candidates_and_summary_totals_rows(self):
        reservation, ledger = self.reservation()
        TicketLedger.objects.filter(pk=ledger.pk).update(created_at=self.now)
        for offset in (2, 3):
            TicketPurchase.objects.create(user=self.user, total_tickets=1, remaining_tickets=0,
                unit_price=3500, purchased_at=self.now - timedelta(days=offset))
        stdout = StringIO()
        call_command("audit_missing_ticket_purchase_evidence", "--reservation-id", str(reservation.id), stdout=stdout)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["rows"][0]["candidate_classification"], "multiple_purchase_candidates")
        summary = result["summary"]
        classified = sum(summary[key] for key in ("no_purchase_candidate_count", "multiple_purchase_candidate_count",
            "single_but_inconsistent_count", "price_evidence_missing_count", "legacy_only_count", "other_count"))
        self.assertEqual(classified, summary["missing_count"])

    def test_reports_later_purchase_fifo_candidate_without_writes(self):
        reservation, _ = self.reservation()
        purchase = TicketPurchase.objects.create(
            user=self.user, total_tickets=4, remaining_tickets=4,
            unit_price=500, purchase_type=TicketPurchase.PURCHASE_TYPE_ADMIN,
            purchased_at=self.now,
        )
        stdout = StringIO()
        with CaptureQueriesContext(connection) as queries:
            call_command("audit_missing_ticket_purchase_evidence", "--reservation-id", str(reservation.id), stdout=stdout)
        row = json.loads(stdout.getvalue())["rows"][0]
        self.assertEqual(row["candidate_later_purchase_id"], purchase.id)
        self.assertEqual(row["candidate_purchase_unit_price"], 500)
        self.assertEqual(row["fifo_position"], 1)
        self.assertTrue(row["linkage_possible"])
        self.assertIsNone(row["block_reason"])
        self.assertTrue(all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries.captured_queries))
