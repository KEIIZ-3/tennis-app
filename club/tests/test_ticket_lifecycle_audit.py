import json
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import TestCase

from club.models import TicketLedger, TicketPurchase, User
from club.ticket_lifecycle_audit import diagnose_ticket_lifecycle


class TicketLifecycleAuditTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="audit-member", password="x", ticket_balance=4)
        TicketPurchase.objects.create(
            user=self.user, purchase_type=TicketPurchase.PURCHASE_TYPE_SET4,
            total_tickets=4, remaining_tickets=4, unit_price=2500,
            idempotency_key="admin-set4:secret-token:1",
        )

    def test_audit_is_select_only_and_does_not_disclose_key(self):
        before = self._state()
        statements = []

        def capture(execute, sql, params, many, context):
            statements.append(sql.lstrip().split(None, 1)[0].upper())
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            result = diagnose_ticket_lifecycle()
        self.assertEqual(before, self._state())
        self.assertEqual(set(statements), {"SELECT", "PRAGMA"})
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("secret-token", rendered)
        self.assertEqual(result["idempotency"]["prefix_counts"], {"admin-set4:": 1})
        self.assertEqual(result["idempotency"]["duplicate_non_null_key_group_count"], 0)
        self.assertTrue(result["idempotency"]["unique_constraint_present"])

    def test_management_command_returns_json(self):
        stdout = StringIO()
        call_command("audit_ticket_lifecycle", stdout=stdout)
        self.assertTrue(json.loads(stdout.getvalue())["read_only"])

    def _state(self):
        return {
            "users": list(User.objects.values_list("id", "ticket_balance")),
            "purchases": list(TicketPurchase.objects.values_list("id", "remaining_tickets", "idempotency_key")),
            "ledgers": list(TicketLedger.objects.values_list("id", "change_amount", "balance_after")),
        }
