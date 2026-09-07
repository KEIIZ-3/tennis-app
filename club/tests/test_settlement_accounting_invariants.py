from types import SimpleNamespace

from django.test import TestCase
from django.utils import timezone

from club.models import MAIN_COACH_NAMES, ShopPurchase, User
from club.settlement_calculator import split_revenue_amount
from club.shop_service import (
    cancel_purchase, monthly_shop_allocations,
    monthly_shop_procurement_reimbursements, save_allocations,
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
