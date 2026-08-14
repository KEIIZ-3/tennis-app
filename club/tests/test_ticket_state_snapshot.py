import hashlib
import json
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from club.models import (
    Court,
    FamilyMember,
    Reservation,
    TicketConsumption,
    TicketLedger,
    TicketPurchase,
    User,
)
from club.ticket_state_snapshot import build_ticket_state_snapshot


class TicketStateSnapshotTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user(
            username="private-user",
            email="private@example.com",
            password="unused",
            full_name="Private Person",
            phone_number="090-private",
            ticket_balance=4,
        )
        self.coach = User.objects.create_user(
            username="private-coach", password="unused", role=User.ROLE_COACH
        )
        self.court = Court.objects.create(name="Private Court")
        self.now = timezone.now()

    def purchase(self, *, total=4, remaining=4, legacy=False, note="private note"):
        return TicketPurchase.objects.create(
            user=self.member,
            purchase_type=(
                TicketPurchase.PURCHASE_TYPE_LEGACY
                if legacy
                else TicketPurchase.PURCHASE_TYPE_SET4
            ),
            total_tickets=total,
            remaining_tickets=remaining,
            unit_price=3500,
            label="private label",
            note=note,
            purchased_at=self.now,
        )

    def reservation(self, *, tickets=1, consumed=True, refunded=False):
        row = Reservation(
            user=self.member,
            coach=self.coach,
            court=self.court,
            start_at=self.now + timedelta(days=1),
            end_at=self.now + timedelta(days=1, hours=1),
            lesson_type=Reservation.LESSON_PRIVATE,
            tickets_used=tickets,
            participant_ticket_price_snapshot=3500 if tickets else None,
            ticket_consumed_at=self.now if consumed else None,
            ticket_refunded_at=self.now if refunded else None,
        )
        Reservation.objects.bulk_create([row])
        return row

    def test_snapshot_contains_persisted_and_derived_ticket_state_by_lot(self):
        first = self.purchase(total=4, remaining=1)
        second = self.purchase(total=3, remaining=3, legacy=True)
        reservation = self.reservation()
        consumption = TicketConsumption.objects.create(
            user=self.member,
            purchase=first,
            reservation=reservation,
            tickets_used=1,
            unit_price_snapshot=3500,
            refunded_at=self.now,
            refund_note="private refund note",
        )
        ledger = TicketLedger.objects.create(
            user=self.member,
            reservation=reservation,
            change_amount=-1,
            balance_after=4,
            reason=TicketLedger.REASON_RESERVATION_USE,
            note="private ledger note",
        )

        result = build_ticket_state_snapshot()
        user = next(row for row in result["users"] if row["user_id"] == self.member.id)
        self.assertEqual(user["persisted"], {"ticket_balance": 4})
        self.assertEqual(user["derived"]["purchase_remaining_total"], 4)
        self.assertEqual(user["derived"]["active_purchase_count"], 2)
        self.assertEqual(user["derived"]["consumption_count"], 1)
        self.assertEqual(user["derived"]["refunded_consumption_count"], 1)
        self.assertEqual(user["derived"]["ledger_count"], 1)
        self.assertEqual(
            [row["purchase_id"] for row in result["purchases"]],
            [first.id, second.id],
        )
        self.assertEqual(result["purchases"][1]["purchase_type"], "legacy")
        self.assertEqual(result["consumptions"][0]["consumption_id"], consumption.id)
        self.assertIsNotNone(result["consumptions"][0]["refunded_at"])
        self.assertEqual(result["ledgers"][0]["ledger_id"], ledger.id)
        self.assertEqual(result["reservations"][0]["reservation_id"], reservation.id)

    def test_zero_ticket_reservation_and_family_account_have_no_pii(self):
        self.purchase(note="do not output this purchase note")
        reservation = self.reservation(tickets=0, consumed=False)
        FamilyMember.objects.create(
            parent=self.member,
            full_name="Private Child",
            relationship="child",
        )
        output = json.dumps(build_ticket_state_snapshot(), ensure_ascii=False)
        self.assertIn(f'"reservation_id": {reservation.id}', output)
        for secret in (
            "private-user",
            "private@example.com",
            "Private Person",
            "090-private",
            "Private Child",
            "private note",
            "private label",
            "private refund note",
            "private ledger note",
            "Private Court",
        ):
            self.assertNotIn(secret, output)

    def test_command_is_deterministic_select_only_and_does_not_change_any_field(self):
        purchase = self.purchase(total=2, remaining=1)
        reservation = self.reservation(refunded=True)
        TicketConsumption.objects.create(
            user=self.member,
            purchase=purchase,
            reservation=reservation,
            tickets_used=1,
            unit_price_snapshot=3500,
            refunded_at=self.now,
        )
        TicketLedger.objects.create(
            user=self.member,
            reservation=reservation,
            change_amount=1,
            balance_after=4,
            reason=TicketLedger.REASON_CANCEL_REFUND,
        )
        models = (User, TicketPurchase, TicketConsumption, TicketLedger, Reservation)
        before = {model.__name__: list(model.objects.order_by("pk").values()) for model in models}
        statements = []

        def capture(execute, sql, params, many, context):
            statements.append(sql.lstrip().split(None, 1)[0].upper())
            return execute(sql, params, many, context)

        outputs = []
        with connection.execute_wrapper(capture):
            for _ in range(2):
                stdout = StringIO()
                call_command("snapshot_ticket_state", stdout=stdout)
                outputs.append(stdout.getvalue())
        after = {model.__name__: list(model.objects.order_by("pk").values()) for model in models}
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(before, after)
        self.assertEqual(set(statements), {"SELECT"})
        parsed = json.loads(outputs[0])
        fingerprint = parsed.pop("fingerprint")
        canonical = json.dumps(
            parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.assertEqual(fingerprint["value"], hashlib.sha256(canonical).hexdigest())

    def test_query_count_is_constant(self):
        for index in range(5):
            self.purchase(total=index + 1, remaining=index + 1)
            self.reservation(tickets=0, consumed=False)
        with self.assertNumQueries(5):
            build_ticket_state_snapshot()
