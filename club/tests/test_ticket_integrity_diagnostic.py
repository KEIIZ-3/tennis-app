import json
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from club.models import (
    Court,
    Reservation,
    ReservationParticipant,
    TicketConsumption,
    TicketLedger,
    TicketPurchase,
    User,
    purchase_tickets,
)
from club.ticket_integrity_diagnostic import diagnose_ticket_integrity


class TicketIntegrityDiagnosticTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user(
            username="secret-member", email="secret@example.com", password="unused",
            full_name="Private Person", phone_number="090-1111-2222",
        )
        self.coach = User.objects.create_user(username="coach", password="unused", role=User.ROLE_COACH)
        self.other = User.objects.create_user(username="other-secret", password="unused")
        self.court = Court.objects.create(name="Diagnostic court")
        self.start = timezone.now() + timedelta(days=30)

    def reservation(self, *, tickets=1, status=Reservation.STATUS_ACTIVE, user=None):
        reservation = Reservation(
            user=user or self.member, coach=self.coach, court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE, start_at=self.start,
            end_at=self.start + timedelta(hours=1), tickets_used=tickets, status=status,
        )
        Reservation.objects.bulk_create([reservation])
        return reservation

    def purchase(self, *, user=None, total=4, remaining=4, legacy=True):
        return TicketPurchase.objects.create(
            user=user or self.member,
            purchase_type=(TicketPurchase.PURCHASE_TYPE_LEGACY if legacy else TicketPurchase.PURCHASE_TYPE_SET4),
            total_tickets=total, remaining_tickets=remaining, unit_price=3500,
        )

    def test_normal_purchase_consume_refund_is_not_detected(self):
        purchase_tickets(
            user=self.member, tickets=4, unit_price=3500,
            purchase_type=TicketPurchase.PURCHASE_TYPE_SET4,
            reason=TicketLedger.REASON_PURCHASE_SET4,
        )
        reservation = self.reservation()
        reservation.consume_tickets()
        reservation.cancel(reason="private cancellation text")

        result = diagnose_ticket_integrity()
        self.assertEqual(result["finding_count"], 0)

    def test_proven_balance_purchase_and_consumption_mismatches(self):
        purchase = self.purchase(total=1, remaining=2)
        self.member.ticket_balance = 9
        self.member.save(update_fields=["ticket_balance"])
        reservation = self.reservation(tickets=2)
        TicketConsumption.objects.create(
            user=self.other, purchase=purchase, reservation=reservation,
            tickets_used=1, unit_price_snapshot=3500,
        )

        result = diagnose_ticket_integrity()
        self.assertEqual(result["purchase_findings"][0]["reason"], "remaining_exceeds_total")
        self.assertEqual(result["balance_findings"][0]["reason"], "balance_purchase_remaining_mismatch")
        reasons = {row["reason"] for row in result["consumption_findings"]}
        self.assertIn("purchase_user_mismatch", reasons)
        self.assertIn("reservation_user_mismatch", reasons)
        self.assertEqual(result["reservation_findings"][0]["reason"], "consumption_without_consumed_at")
        self.assertTrue(any(row["reason"] == "reservation_consumption_ticket_mismatch" for row in result["reservation_findings"]))

    def test_zero_ticket_and_missing_legacy_baseline_are_not_findings(self):
        self.member.ticket_balance = 3
        self.member.save(update_fields=["ticket_balance"])
        self.reservation(tickets=0)

        result = diagnose_ticket_integrity()
        self.assertEqual(result["finding_count"], 0)
        self.assertEqual(result["special_cases"]["reservation"]["zero_ticket"], 1)
        self.assertEqual(result["unverifiable"]["balance"][0]["reason"], "no_legacy_baseline")

    def test_family_pii_is_not_output(self):
        reservation = self.reservation(tickets=0)
        ReservationParticipant.objects.create(
            reservation=reservation, parent=self.member, participant_type="family",
            participant_name="Secret Child Name",
        )
        output = json.dumps(diagnose_ticket_integrity(), ensure_ascii=False, sort_keys=True)
        self.assertEqual(json.loads(output)["special_cases"]["reservation"]["family"], 1)
        for secret in ("secret-member", "secret@example.com", "Private Person", "090-1111-2222", "Secret Child Name"):
            self.assertNotIn(secret, output)

    def test_command_is_deterministic_and_read_only(self):
        purchase = self.purchase(total=1, remaining=1)
        reservation = self.reservation()
        TicketConsumption.objects.create(
            user=self.member, purchase=purchase, reservation=reservation,
            tickets_used=1, unit_price_snapshot=3500,
        )
        models = (User, Reservation, TicketPurchase, TicketConsumption, TicketLedger)
        before = {model.__name__: list(model.objects.order_by("pk").values()) for model in models}
        outputs = []
        statements = []

        def capture(execute, sql, params, many, context):
            statements.append(sql.lstrip().split(None, 1)[0].upper())
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            for _ in range(2):
                stdout = StringIO()
                call_command("diagnose_ticket_integrity", stdout=stdout)
                outputs.append(stdout.getvalue())
        after = {model.__name__: list(model.objects.order_by("pk").values()) for model in models}
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(before, after)
        self.assertEqual(set(statements), {"SELECT"})
        json.loads(outputs[0])

    def test_query_count_is_constant(self):
        for _ in range(5):
            self.reservation(tickets=0)
        with self.assertNumQueries(6):
            diagnose_ticket_integrity()
