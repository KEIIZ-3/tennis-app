from datetime import datetime
from io import StringIO
import tempfile

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from club.models import TicketCashReceipt, TicketPurchase, User


class TicketCashReceiptRepairCommandTests(TestCase):
    def setUp(self):
        self.actor = User.objects.create_user(username="receipt-repair-actor")
        self.member = User.objects.create_user(username="receipt-repair-member", full_name="Repair Member")

    def _purchase(self, amount, purchased_at):
        return TicketPurchase.objects.create(
            user=self.member, purchase_type=TicketPurchase.PURCHASE_TYPE_ADMIN,
            total_tickets=1, remaining_tickets=1, unit_price=amount,
            purchased_at=timezone.make_aware(purchased_at),
        )

    def _csv(self, rows):
        source = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", encoding="utf-8", newline="", delete=False)
        source.write("ticket_purchase_id,amount,received_at,payment_method\n")
        for row in rows:
            source.write(",".join(map(str, row)) + "\n")
        source.close()
        self.addCleanup(lambda: __import__("os").unlink(source.name))
        return source.name

    def test_preview_is_read_only_and_apply_uses_explicit_received_dates(self):
        purchases = [
            self._purchase(500, datetime(2026, 7, 10, 12)),
            self._purchase(500, datetime(2026, 7, 11, 12)),
            self._purchase(42000, datetime(2026, 8, 10, 12)),
            self._purchase(42000, datetime(2026, 8, 11, 12)),
        ]
        path = self._csv([
            (purchases[0].pk, 500, "2026-08-02T12:00:00+09:00", "cash"),
            (purchases[1].pk, 500, "2026-08-02T12:00:00+09:00", "cash"),
            (purchases[2].pk, 42000, "2026-08-20T12:00:00+09:00", "cash"),
            (purchases[3].pk, 42000, "2026-08-20T12:00:00+09:00", "cash"),
        ])
        preview = StringIO()
        call_command("repair_ticket_cash_receipts", path, created_by=self.actor.pk, stdout=preview)
        self.assertEqual(TicketCashReceipt.objects.count(), 0)
        self.assertIn("existing_receipt=no can_apply=yes", preview.getvalue())

        call_command("repair_ticket_cash_receipts", path, created_by=self.actor.pk, apply=True)
        august = TicketCashReceipt.objects.filter(received_at__year=2026, received_at__month=8)
        self.assertEqual(sum(receipt.amount for receipt in august), 85000)

        repeated = StringIO()
        call_command("repair_ticket_cash_receipts", path, created_by=self.actor.pk, apply=True, stdout=repeated)
        self.assertEqual(TicketCashReceipt.objects.count(), 4)
        self.assertIn("existing_receipt=yes can_apply=no", repeated.getvalue())
