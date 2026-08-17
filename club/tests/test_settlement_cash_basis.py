from datetime import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from club.models import TicketPurchase
from club.settlement_balance_policy import _company_cash_in_total
from club.settlement_loader import load_monthly_settlement_data
from club.settlement_totals import calculate_settlement_totals


class SettlementCashBasisTests(TestCase):
    def test_purchase_cash_is_separate_from_consumption_revenue(self):
        total = _company_cash_in_total(
            {"ticket_purchase_total": 14000, "ticket_amount_total": 3500},
            [{"ticket_amount": 3500, "preopen_paid_amount": 0, "stringing_amount": 0}],
        )
        self.assertEqual(total, 14000)

    def test_totals_use_saved_lot_amount_and_do_not_readd_consumption(self):
        purchase = TicketPurchase(total_tickets=4, unit_price=3500)
        totals = calculate_settlement_totals(
            coach_rows=[{
                "preopen_paid_amount": 0, "preopen_unpaid_amount": 0,
                "ticket_amount": 3500, "salary_due": 0,
                "reimbursement_due": 0, "salary_paid": 0,
                "reimbursement_paid": 0, "unpaid_salary": 0,
                "unpaid_reimbursement": 0,
            }],
            ticket_purchases=[purchase], stringing_total=0,
            approved_common_expense_total=0,
            submitted_personal_expense_rows=[],
            expense_approval_submitted="submitted", money=int,
        )
        self.assertEqual(totals["ticket_purchase_total"], 14000)
        self.assertEqual(totals["ticket_amount_total"], 3500)
        self.assertEqual(totals["cash_in_total"], 14000)

    def test_loader_excludes_legacy_synthetic_purchase_from_cash(self):
        user = get_user_model().objects.create_user(username="cash-audit-member")
        purchased_at = timezone.make_aware(datetime(2026, 8, 3, 12, 0))
        real = TicketPurchase.objects.create(
            user=user, purchase_type=TicketPurchase.PURCHASE_TYPE_SET4,
            total_tickets=4, remaining_tickets=4, unit_price=3500,
            purchased_at=purchased_at,
        )
        TicketPurchase.objects.create(
            user=user, purchase_type=TicketPurchase.PURCHASE_TYPE_LEGACY,
            total_tickets=4, remaining_tickets=4, unit_price=3500,
            purchased_at=purchased_at,
        )
        data = load_monthly_settlement_data(
            month_start=datetime(2026, 8, 1).date(),
            next_month=datetime(2026, 9, 1).date(),
        )
        self.assertEqual([row.pk for row in data["ticket_purchases"]], [real.pk])

    def test_recalculation_does_not_accumulate_purchase_cash(self):
        result = {"ticket_purchase_total": 14000}
        rows = [{"preopen_paid_amount": 4000, "stringing_amount": 1200}]
        self.assertEqual(_company_cash_in_total(result, rows), 19200)
        self.assertEqual(_company_cash_in_total(result, rows), 19200)
