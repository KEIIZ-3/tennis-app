from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.models import (MAIN_COACH_NAMES, ShopPurchase, ShopQuote,
                         ShopRevenueAllocation, User)
from club.settlement_models import MonthlySettlement
from club.settlement_calculator import split_revenue_amount
from club.shop_service import (
    cancel_purchase, confirm_quote_purchase, create_quote,
    monthly_shop_allocations, monthly_shop_cash_total,
    monthly_shop_procurement_reimbursements, rollback_purchase_to_quote,
    save_allocations,
)


class LessonRevenueSplitInvariantTests(TestCase):
    def coach(self, pk, name, role="coach"):
        return SimpleNamespace(pk=pk, full_name=name, role=role,
                               display_name=lambda: name)

    def test_iizuka_gets_remainder_and_total_is_preserved(self):
        coaches = [self.coach(30, MAIN_COACH_NAMES[2]),
                   self.coach(10, MAIN_COACH_NAMES[0]),
                   self.coach(20, MAIN_COACH_NAMES[1])]
        self.assertEqual(split_revenue_amount(4000, coaches),
                         {10: 1334, 20: 1333, 30: 1333})
        for amount in (4001, 4002, 3999):
            self.assertEqual(sum(split_revenue_amount(amount, coaches).values()), amount)

    def test_non_iizuka_remainder_stays_with_stable_attending_coach(self):
        shimizu = self.coach(20, MAIN_COACH_NAMES[1])
        contractor = self.coach(40, "業務委託", "contractor_coach")
        result = split_revenue_amount(4001, [contractor, shimizu])
        self.assertEqual(result, {20: 2001, 40: 2000})
        self.assertNotIn(10, result)


class ShopAccountingInvariantTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user("shop-customer")
        self.coaches = [User.objects.create_user(
            f"main-{index}", role=User.ROLE_COACH, full_name=name
        ) for index, name in enumerate(MAIN_COACH_NAMES)]
        self.admin = User.objects.create_superuser("shop-admin")
        self.purchase = ShopPurchase.objects.create(
            customer=self.customer, registered_by=self.admin,
            description="ラケット", quantity=1, amount=14000,
        )

    def test_sale_equals_procurement_reimbursement_plus_profit_allocations(self):
        summary = save_allocations(
            purchase=self.purchase, actor=self.admin, purchase_cost=10000,
            procurement_coach=self.coaches[0], amounts={
                self.coaches[0].pk: 2000,
                self.coaches[1].pk: 1000,
                self.coaches[2].pk: 1000,
            })
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.profit_amount_snapshot, 4000)
        self.assertEqual(float(self.purchase.profit_rate_snapshot), 28.571)
        self.assertEqual(summary["allocated"], 4000)
        month = timezone.localdate()
        profits = monthly_shop_allocations(month.year, month.month)
        reimbursements = monthly_shop_procurement_reimbursements(month.year, month.month)
        self.assertEqual(profits, {self.coaches[0].pk: 2000,
                                  self.coaches[1].pk: 1000,
                                  self.coaches[2].pk: 1000})
        self.assertEqual(reimbursements, {self.coaches[0].pk: 10000})
        self.assertEqual(sum(profits.values()) + sum(reimbursements.values()), 14000)
        cancel_purchase(purchase=self.purchase, actor=self.admin)
        self.assertEqual(monthly_shop_allocations(month.year, month.month), {})
        self.assertEqual(monthly_shop_procurement_reimbursements(month.year, month.month), {})

    def make_confirmed_quote(self, *, sale, cost, allocation=None):
        allocation = sale - cost if allocation is None else allocation
        quote = create_quote(
            customer=self.customer, creator=self.admin,
            items=[{"description": "ラケット", "quantity": 1,
                    "list_price": sale, "sale_price": sale, "cost_price": cost}],
            accounting={"procurement_coach": self.coaches[0].pk,
                        "amounts": {coach.pk: allocation if index == 0 else 0
                                    for index, coach in enumerate(self.coaches)}},
        )
        purchase, created = confirm_quote_purchase(quote=quote, actor=self.admin)
        self.assertTrue(created)
        return quote, purchase

    def test_rollback_and_repurchase_never_double_count_monthly_settlement(self):
        month = timezone.localdate()
        quote, old_purchase = self.make_confirmed_quote(sale=33825, cost=26053)
        self.assertEqual(monthly_shop_cash_total(month.year, month.month), 33825)
        self.assertEqual(sum(monthly_shop_procurement_reimbursements(
            month.year, month.month).values()), 26053)
        self.assertEqual(sum(monthly_shop_allocations(month.year, month.month).values()), 7772)
        rollback_purchase_to_quote(
            purchase=old_purchase, actor=self.admin, reason="販売価格誤り",
        )
        self.assertEqual(monthly_shop_cash_total(month.year, month.month), 0)
        self.assertEqual(monthly_shop_procurement_reimbursements(month.year, month.month), {})
        self.assertEqual(monthly_shop_allocations(month.year, month.month), {})

        _, other_purchase = self.make_confirmed_quote(sale=10000, cost=8000)
        old_purchase.refresh_from_db()
        quote.refresh_from_db()
        self.assertEqual(old_purchase.status, ShopPurchase.STATUS_REVERTED)
        self.assertEqual(quote.status, ShopQuote.STATUS_SENT)
        self.assertTrue(ShopPurchase.objects.filter(pk=old_purchase.pk).exists())
        self.assertTrue(ShopRevenueAllocation.objects.filter(purchase=old_purchase).exists())
        audit = old_purchase.allocation_audits.get(event_type="rollback")
        self.assertEqual(audit.reason, "販売価格誤り")
        self.assertEqual(audit.previous_snapshot["sale_amount"], 33825)
        self.assertEqual(monthly_shop_cash_total(month.year, month.month), 10000)
        self.assertEqual(sum(monthly_shop_procurement_reimbursements(
            month.year, month.month).values()), 8000)
        self.assertEqual(sum(monthly_shop_allocations(month.year, month.month).values()), 2000)

        item = quote.items.get()
        item.list_price = item.sale_price = 30000
        item.cost_price = 25000
        item.save(update_fields=["list_price", "sale_price", "cost_price"])
        quote.accounting_sale_amount = 30000
        quote.accounting_purchase_cost = 25000
        quote.planned_profit_allocations = {
            str(coach.pk): 5000 if index == 0 else 0
            for index, coach in enumerate(self.coaches)
        }
        quote.save(update_fields=["accounting_sale_amount", "accounting_purchase_cost",
                                  "planned_profit_allocations"])
        new_purchase, created = confirm_quote_purchase(quote=quote, actor=self.admin)
        self.assertTrue(created)
        self.assertNotEqual(new_purchase.pk, old_purchase.pk)
        self.assertEqual(ShopPurchase.objects.filter(
            quote=quote, status=ShopPurchase.STATUS_CONFIRMED).count(), 1)
        self.assertEqual((old_purchase.amount, old_purchase.cost_total,
                          old_purchase.profit_amount_snapshot), (33825, 26053, 7772))
        self.assertEqual(monthly_shop_cash_total(month.year, month.month), 40000)
        self.assertEqual(sum(monthly_shop_procurement_reimbursements(
            month.year, month.month).values()), 33000)
        self.assertEqual(sum(monthly_shop_allocations(month.year, month.month).values()), 7000)

    def test_closed_purchase_month_cannot_be_rolled_back(self):
        quote, purchase = self.make_confirmed_quote(sale=30000, cost=25000)
        local_date = timezone.localdate(purchase.purchased_at)
        MonthlySettlement.objects.create(
            year=local_date.year, month=local_date.month,
            status=MonthlySettlement.STATUS_CLOSED,
        )
        with self.assertRaises(ValidationError):
            rollback_purchase_to_quote(
                purchase=purchase, actor=self.admin, reason="原価修正",
            )
        purchase.refresh_from_db()
        quote.refresh_from_db()
        self.assertEqual(purchase.status, ShopPurchase.STATUS_CONFIRMED)
        self.assertEqual(quote.status, ShopQuote.STATUS_PURCHASED)
        self.assertFalse(purchase.allocation_audits.filter(event_type="rollback").exists())

    def test_rollback_ui_and_endpoint_are_admin_only(self):
        quote, purchase = self.make_confirmed_quote(sale=30000, cost=25000)
        detail = reverse("club:shop_quote_detail", args=[quote.pk])
        action = reverse("club:shop_purchase_rollback", args=[purchase.pk])
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(detail), "見積へ差し戻す")
        self.assertContains(self.client.get(action), "差し戻し理由")
        for user in (self.coaches[0], self.customer):
            self.client.force_login(user)
            self.assertNotContains(self.client.get(detail), "見積へ差し戻す")
            self.assertEqual(self.client.get(action).status_code, 403)
            self.assertEqual(self.client.post(action, {"reason": "価格誤り"}).status_code, 403)

    def test_rollback_reason_is_required(self):
        quote, purchase = self.make_confirmed_quote(sale=30000, cost=25000)
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("club:shop_purchase_rollback", args=[purchase.pk]), {"reason": " "},
        )
        self.assertEqual(response.status_code, 200)
        purchase.refresh_from_db()
        quote.refresh_from_db()
        self.assertEqual(purchase.status, ShopPurchase.STATUS_CONFIRMED)
        self.assertEqual(quote.status, ShopQuote.STATUS_PURCHASED)
        response = self.client.post(
            reverse("club:shop_purchase_rollback", args=[purchase.pk]),
            {"reason": "価格誤り"},
        )
        self.assertRedirects(response, reverse("club:shop_quote_detail", args=[quote.pk]))
        purchase.refresh_from_db()
        quote.refresh_from_db()
        self.assertEqual(purchase.status, ShopPurchase.STATUS_REVERTED)
        self.assertEqual(quote.status, ShopQuote.STATUS_SENT)
        self.assertEqual(
            self.client.get(reverse("club:shop_quote_edit", args=[quote.pk])).status_code, 200,
        )
