import json
from datetime import datetime, timedelta
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from club.legacy_ticket_evidence_diagnostic import diagnose_legacy_ticket_evidence
from club.models import (
    Court, FixedLesson, Reservation, ReservationParticipant, TicketConsumption,
    TicketLedger, TicketPurchase, User, purchase_tickets,
)


class LegacyTicketEvidenceDiagnosticTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user(
            username="secret-user", email="secret@example.com", password="unused",
            full_name="Secret Name", phone_number="090-secret",
        )
        self.coach = User.objects.create_user(username="coach", password="unused", role=User.ROLE_COACH)
        self.court = Court.objects.create(name="court")
        self.start = timezone.now() + timedelta(days=20)

    def reservation(self, *, user=None, tickets=1, status=Reservation.STATUS_ACTIVE,
                    start=None, lesson_type=Reservation.LESSON_PRIVATE, fixed_lesson=None,
                    fixed=False, consumed=True, refunded=False):
        row = Reservation(
            user=user or self.member, coach=self.coach, court=self.court,
            lesson_type=lesson_type, start_at=start or self.start,
            end_at=(start or self.start) + timedelta(hours=1), tickets_used=tickets,
            status=status, fixed_lesson=fixed_lesson, is_fixed_entry=fixed,
            ticket_consumed_at=timezone.now() if consumed else None,
            ticket_refunded_at=timezone.now() if refunded else None,
        )
        Reservation.objects.bulk_create([row])
        return row

    def purchase(self, *, user=None, legacy=False, total=4, remaining=4):
        return TicketPurchase.objects.create(
            user=user or self.member,
            purchase_type=TicketPurchase.PURCHASE_TYPE_LEGACY if legacy else TicketPurchase.PURCHASE_TYPE_SET4,
            total_tickets=total, remaining_tickets=remaining, unit_price=3500,
        )

    def ledger(self, reservation, amount, reason):
        return TicketLedger.objects.create(
            user=reservation.user, reservation=reservation,
            fixed_lesson=reservation.fixed_lesson, change_amount=amount,
            balance_after=reservation.user.ticket_balance, reason=reason,
        )

    def test_exact_ledger_is_recoverable_without_consumption(self):
        reservation = self.reservation(tickets=2)
        ledger = self.ledger(reservation, -2, TicketLedger.REASON_RESERVATION_USE)
        result = diagnose_legacy_ticket_evidence()
        row = result["reservation_evidence"]["rows"][0]
        self.assertEqual(row["classification"], "consumption_proven_by_exact_ledger")
        self.assertEqual(row["recoverability"], "recoverable_accounting_event")
        self.assertEqual(row["consumption_ledger_ids"], [ledger.id])

    def test_fixed_preopen_canceled_refunded_family_and_legacy_are_classified(self):
        fixed = FixedLesson.objects.create(
            coach=self.coach, court=self.court, weekday=self.start.weekday(),
            start_date=self.start.date(), start_hour=19,
        )
        fixed_reservation = self.reservation(fixed_lesson=fixed, fixed=True)
        self.purchase(legacy=True)
        preopen_start = timezone.make_aware(datetime(2026, 7, 10, 10, 0))
        preopen = self.reservation(
            start=preopen_start, lesson_type=Reservation.LESSON_GENERAL,
            status=Reservation.STATUS_CANCELED, refunded=True,
        )
        refund = self.ledger(preopen, 1, TicketLedger.REASON_CANCEL_REFUND)
        ReservationParticipant.objects.create(
            reservation=preopen, parent=self.member, participant_type="family",
            participant_name="Secret Child",
        )

        rows = {row["reservation_id"]: row for row in diagnose_legacy_ticket_evidence()["reservation_evidence"]["rows"]}
        self.assertEqual(rows[fixed_reservation.id]["classification"], "confirmed_legacy_fixed_lesson_shape")
        self.assertEqual(rows[fixed_reservation.id]["recoverability"], "indeterminate")
        self.assertEqual(rows[preopen.id]["classification"], "preopen_with_legacy_ticket_markers")
        self.assertEqual(rows[preopen.id]["refund_ledger_ids"], [refund.id])
        self.assertIn("family_participant_snapshot_exists", rows[preopen.id]["evidence"])

    def test_indeterminate_and_multiple_lots_are_reported_without_false_recovery(self):
        reservation = self.reservation()
        first = self.purchase(legacy=True, total=2, remaining=1)
        second = self.purchase(legacy=True, total=3, remaining=3)
        row = diagnose_legacy_ticket_evidence()["reservation_evidence"]["rows"][0]
        self.assertEqual(row["classification"], "legacy_operation_supported_not_reservation_proven")
        self.assertEqual(row["recoverability"], "indeterminate")
        self.assertEqual(row["legacy_purchase_ids"], [first.id, second.id])

    def test_balance_and_no_ledger_baseline_subclasses(self):
        self.member.ticket_balance = -2
        self.member.save(update_fields=["ticket_balance"])
        purchase = self.purchase(total=2, remaining=1)
        consumption_reservation = self.reservation(consumed=False)
        TicketConsumption.objects.create(
            user=self.member, purchase=purchase, reservation=consumption_reservation,
            tickets_used=1, unit_price_snapshot=3500,
        )
        result = diagnose_legacy_ticket_evidence()
        balance = next(row for row in result["balance_evidence"]["rows"] if row["user_id"] == self.member.id)
        baseline = next(row for row in result["ledger_baseline_evidence"]["rows"] if row["user_id"] == self.member.id)
        self.assertEqual(balance["classification"], "negative_balance")
        self.assertEqual(balance["required_unproven_opening_balance"], -3)
        self.assertEqual(baseline["classification"], "consumption_without_ledger_requires_investigation")

    def test_current_complete_flow_is_not_a_legacy_evidence_row(self):
        purchase_tickets(
            user=self.member, tickets=4, unit_price=3500,
            purchase_type=TicketPurchase.PURCHASE_TYPE_SET4,
            reason=TicketLedger.REASON_PURCHASE_SET4,
        )
        reservation = self.reservation(consumed=False)
        reservation.consume_tickets()
        reservation.cancel()
        result = diagnose_legacy_ticket_evidence()
        self.assertEqual(result["reservation_evidence"]["count"], 0)
        self.assertEqual(result["balance_evidence"]["count"], 0)

    def test_command_is_pii_free_deterministic_read_only_and_constant_query_count(self):
        reservation = self.reservation()
        ReservationParticipant.objects.create(
            reservation=reservation, parent=self.member, participant_type="family",
            participant_name="Secret Child",
        )
        models = (User, Reservation, TicketPurchase, TicketConsumption, TicketLedger)
        before = {m.__name__: list(m.objects.order_by("pk").values()) for m in models}
        statements = []

        def capture(execute, sql, params, many, context):
            statements.append(sql.lstrip().split(None, 1)[0].upper())
            return execute(sql, params, many, context)

        outputs = []
        with connection.execute_wrapper(capture):
            for _ in range(2):
                stdout = StringIO()
                call_command("diagnose_legacy_ticket_evidence", stdout=stdout)
                outputs.append(stdout.getvalue())
        after = {m.__name__: list(m.objects.order_by("pk").values()) for m in models}
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(before, after)
        self.assertEqual(set(statements), {"SELECT"})
        for secret in ("secret-user", "secret@example.com", "Secret Name", "090-secret", "Secret Child"):
            self.assertNotIn(secret, outputs[0])
        json.loads(outputs[0])

        with self.assertNumQueries(6):
            diagnose_legacy_ticket_evidence()
