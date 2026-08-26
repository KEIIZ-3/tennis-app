from datetime import datetime

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from club.models import TicketCashReceipt, TicketConsumption, TicketPurchase
from club.settlement_balance_policy import _company_cash_in_total
from club.settlement_loader import load_monthly_settlement_data
from club.settlement_models import MonthlySettlement
from club.settlement_totals import calculate_settlement_totals
from club.ticket_cash_receipt_service import record_ticket_cash_receipt, reverse_ticket_cash_receipt


def aware(year, month, day):
    return timezone.make_aware(datetime(year, month, day, 12, 0))


class SettlementCashBasisTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.actor = User.objects.create_user(username="cash-actor")
        self.member = User.objects.create_user(username="cash-member")

    def purchase(self, *, purchased_at, tickets=1, unit_price=500):
        return TicketPurchase.objects.create(
            user=self.member, total_tickets=tickets, remaining_tickets=tickets,
            unit_price=unit_price, purchased_at=purchased_at,
        )

    def receipt(self, purchase, *, amount, received_at, key):
        return record_ticket_cash_receipt(
            ticket_purchase=purchase, amount=amount, received_at=received_at,
            created_by=self.actor, idempotency_key=key,
        )[0]

    def month_total(self, year, month):
        start = datetime(year, month, 1).date()
        next_start = datetime(year + (month == 12), month % 12 + 1, 1).date()
        data = load_monthly_settlement_data(month_start=start, next_month=next_start)
        totals = calculate_settlement_totals(
            coach_rows=[], ticket_cash_receipts=data["ticket_cash_receipts"],
            stringing_total=0, approved_common_expense_total=0,
            submitted_personal_expense_rows=[], expense_approval_submitted="submitted", money=int,
        )
        return totals["ticket_purchase_total"]

    def test_receipt_creation_is_idempotent_and_reversal_is_audited(self):
        purchase = self.purchase(purchased_at=aware(2026, 8, 2))
        first, created = record_ticket_cash_receipt(
            ticket_purchase=purchase, amount=500, received_at=aware(2026, 8, 2),
            created_by=self.actor, idempotency_key="receipt-1",
        )
        second, created_again = record_ticket_cash_receipt(
            ticket_purchase=purchase, amount=500, received_at=aware(2026, 8, 2),
            created_by=self.actor, idempotency_key="receipt-1",
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.pk, second.pk)
        receipt, reversed_now = reverse_ticket_cash_receipt(
            receipt_id=first.pk, reversed_by=self.actor, reason="cash refund"
        )
        self.assertTrue(reversed_now)
        self.assertIsNotNone(receipt.reversed_at)

    def test_received_month_not_purchase_month_is_canonical(self):
        purchase = self.purchase(purchased_at=aware(2026, 7, 25))
        self.receipt(purchase, amount=500, received_at=aware(2026, 8, 2), key="cross-month")
        self.assertEqual(self.month_total(2026, 7), 0)
        self.assertEqual(self.month_total(2026, 8), 500)

    def test_two_confirmed_500_yen_receipts_move_one_thousand_to_august(self):
        for index in range(2):
            purchase = self.purchase(purchased_at=aware(2026, 7, 25))
            self.receipt(purchase, amount=500, received_at=aware(2026, 8, 2), key=f"confirmed-{index}")
        self.assertEqual(self.month_total(2026, 7), 0)
        self.assertEqual(self.month_total(2026, 8), 1000)

    def test_set4_cash_is_counted_once_despite_later_consumption(self):
        purchase = self.purchase(purchased_at=aware(2026, 8, 20), tickets=4, unit_price=3500)
        self.receipt(purchase, amount=14000, received_at=aware(2026, 8, 20), key="set4")
        TicketConsumption.objects.create(user=self.member, purchase=purchase, tickets_used=1)
        TicketConsumption.objects.create(user=self.member, purchase=purchase, tickets_used=1)
        self.assertEqual(self.month_total(2026, 8), 14000)
        self.assertEqual(self.month_total(2026, 9), 0)
        self.assertEqual(self.month_total(2026, 10), 0)

    def test_unused_paid_ticket_is_counted(self):
        purchase = self.purchase(purchased_at=aware(2026, 8, 20), tickets=12, unit_price=3500)
        self.receipt(purchase, amount=42000, received_at=aware(2026, 8, 20), key="unused")
        self.assertEqual(self.month_total(2026, 8), 42000)

    def test_consumption_refund_and_purchase_reversal_do_not_reverse_cash(self):
        purchase = self.purchase(purchased_at=aware(2026, 8, 20))
        self.receipt(purchase, amount=500, received_at=aware(2026, 8, 20), key="independent")
        consumption = TicketConsumption.objects.create(user=self.member, purchase=purchase, tickets_used=1)
        consumption.refunded_at = aware(2026, 9, 1)
        consumption.save(update_fields=["refunded_at"])
        purchase.reversed_at = aware(2026, 9, 1)
        purchase.save(update_fields=["reversed_at"])
        self.assertEqual(self.month_total(2026, 8), 500)

    def test_receipt_reversal_removes_cash_from_received_month(self):
        purchase = self.purchase(purchased_at=aware(2026, 8, 20))
        receipt = self.receipt(purchase, amount=500, received_at=aware(2026, 8, 20), key="refund")
        reverse_ticket_cash_receipt(receipt_id=receipt.pk, reversed_by=self.actor, reason="refund")
        self.assertEqual(self.month_total(2026, 8), 0)

    def test_closed_received_month_blocks_receipt_changes(self):
        MonthlySettlement.objects.create(year=2026, month=8, status=MonthlySettlement.STATUS_CLOSED)
        purchase = self.purchase(purchased_at=aware(2026, 7, 20))
        with self.assertRaises(ValidationError):
            self.receipt(purchase, amount=500, received_at=aware(2026, 8, 20), key="closed")

    def test_company_cash_total_uses_same_receipt_total(self):
        result = {"ticket_purchase_total": 14000, "ticket_amount_total": 3500}
        self.assertEqual(_company_cash_in_total(result, []), 14000)
