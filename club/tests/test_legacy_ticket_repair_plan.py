import json
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from club.legacy_ticket_repair_plan import diagnose_legacy_ticket_repair_plan
from club.models import Court, Reservation, ReservationParticipant, TicketLedger, TicketPurchase, User


class LegacyTicketRepairPlanTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user(username="private-user", password="x", email="private@example.com", full_name="Private Name", ticket_balance=1)
        self.coach = User.objects.create_user(username="coach", password="x", role=User.ROLE_COACH)
        self.court = Court.objects.create(name="court")

    def reservation(self, *, tickets=1, snapshot=3500, status=Reservation.STATUS_ACTIVE, refunded=False, fixed=False):
        now = timezone.now()
        row = Reservation(
            user=self.member, coach=self.coach, court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE, start_at=now + timedelta(days=1),
            end_at=now + timedelta(days=1, hours=1), tickets_used=tickets,
            participant_ticket_price_snapshot=snapshot, status=status,
            is_fixed_entry=fixed, ticket_consumed_at=now,
            ticket_refunded_at=now if refunded else None,
        )
        Reservation.objects.bulk_create([row])
        return row

    def purchase(self, *, price=3500, remaining=1, total=2):
        return TicketPurchase.objects.create(user=self.member, purchase_type=TicketPurchase.PURCHASE_TYPE_LEGACY, total_tickets=total, remaining_tickets=remaining, unit_price=price, purchased_at=timezone.now() - timedelta(days=2))

    def ledger(self, reservation, amount, reason):
        return TicketLedger.objects.create(user=self.member, reservation=reservation, change_amount=amount, balance_after=self.member.ticket_balance, reason=reason)

    def row(self, reservation):
        rows = diagnose_legacy_ticket_repair_plan()["reservation_plan"]["rows"]
        return next(row for row in rows if row["reservation_id"] == reservation.id)

    def test_exact_ledger_unique_lot_and_price_is_fully_repairable(self):
        reservation = self.reservation()
        purchase = self.purchase()
        self.ledger(reservation, -1, TicketLedger.REASON_RESERVATION_USE)
        row = self.row(reservation)
        self.assertEqual(row["classification"], "fully_repairable_a")
        self.assertEqual(row["repair_payload"]["consumptions"][0]["purchase_id"], purchase.id)
        self.assertFalse(row["repair_payload"]["ledger_change_required"])

    def test_two_lots_or_unknown_price_stays_partial(self):
        reservation = self.reservation()
        self.purchase(remaining=0)
        self.purchase(remaining=1)
        self.ledger(reservation, -1, TicketLedger.REASON_RESERVATION_USE)
        row = self.row(reservation)
        self.assertEqual(row["classification"], "partial_evidence_b")
        self.assertIn("unique_purchase_lot", row["missing_required_evidence"])
        self.assertIsNone(row["repair_payload"])

        self.member.ticket_purchases.all().delete()
        self.purchase(price=0)
        row = self.row(reservation)
        self.assertIn("unit_price_snapshot", row["missing_required_evidence"])

    def test_refund_requires_one_persisted_refund_timestamp(self):
        reservation = self.reservation(status=Reservation.STATUS_CANCELED, refunded=True)
        self.member.ticket_balance = 2
        self.member.save(update_fields=["ticket_balance"])
        self.purchase(remaining=2)
        self.ledger(reservation, -1, TicketLedger.REASON_RESERVATION_USE)
        refund = self.ledger(reservation, 1, TicketLedger.REASON_CANCEL_REFUND)
        row = self.row(reservation)
        self.assertEqual(row["classification"], "fully_repairable_a")
        self.assertEqual(row["repair_payload"]["consumptions"][0]["refunded_at"], refund.created_at.isoformat())

        refund.delete()
        row = self.row(reservation)
        self.assertEqual(row["classification"], "partial_evidence_b")
        self.assertIn("unique_refund_timestamp", row["missing_required_evidence"])

    def test_fixed_or_insufficient_evidence_is_forbidden(self):
        fixed_shape = self.reservation(fixed=True)
        row = self.row(fixed_shape)
        self.assertEqual(row["classification"], "repair_forbidden_c")
        self.assertEqual(row["repair_forbidden_reason"], "no_accounting_event_evidence")

    def test_family_uses_parent_accounting_user_and_multi_ticket_total_is_not_unit_price(self):
        reservation = self.reservation(tickets=2, snapshot=7000)
        ReservationParticipant.objects.create(reservation=reservation, parent=self.member, participant_type="family", participant_name="Private Child")
        self.member.ticket_balance = 0
        self.member.save(update_fields=["ticket_balance"])
        self.purchase(remaining=0)
        self.ledger(reservation, -2, TicketLedger.REASON_RESERVATION_USE)
        row = self.row(reservation)
        self.assertEqual(row["user_id"], self.member.id)
        self.assertEqual(row["classification"], "fully_repairable_a")

        Reservation.objects.filter(pk=reservation.pk).update(participant_ticket_price_snapshot=3500)
        row = self.row(reservation)
        self.assertEqual(row["classification"], "partial_evidence_b")
        self.assertIn("participant_price_snapshot_consistency", row["missing_required_evidence"])

    def test_command_is_select_only_deterministic_pii_free_and_constant_query_count(self):
        reservation = self.reservation()
        self.purchase()
        self.ledger(reservation, -1, TicketLedger.REASON_RESERVATION_USE)
        statements = []
        def capture(execute, sql, params, many, context):
            statements.append(sql.lstrip().split(None, 1)[0].upper())
            return execute(sql, params, many, context)
        outputs = []
        with connection.execute_wrapper(capture):
            for _ in range(2):
                stdout = StringIO()
                call_command("diagnose_legacy_ticket_repair_plan", stdout=stdout)
                outputs.append(stdout.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(set(statements), {"SELECT"})
        json.loads(outputs[0])
        for secret in ("private-user", "private@example.com", "Private Name"):
            self.assertNotIn(secret, outputs[0])
        with self.assertNumQueries(11):
            diagnose_legacy_ticket_repair_plan()
