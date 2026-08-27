from datetime import date

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.models import (ShopInquiry, ShopPurchase, ShopQuote,
                         ShopRevenueAllocation, User)
from club.shop_pdf import build_quote_pdf
from club.shop_service import (allocation_summary, confirm_quote_purchase,
    create_direct_purchase, create_inquiry, create_quote, monthly_shop_allocations,
    one_month_after, request_purchase, save_allocations)


class ShopWorkflowTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user("shop-customer", password="pw", full_name="顧客 太郎")
        self.other = User.objects.create_user("shop-other", password="pw")
        self.coach = User.objects.create_user("shop-coach", password="pw", role=User.ROLE_COACH)
        self.admin = User.objects.create_superuser("shop-admin", password="pw")

    def test_customer_can_submit_free_text_and_empty_is_rejected(self):
        self.client.force_login(self.customer)
        response = self.client.post(reverse("club:shop_estimate"), {"wanted_item": " HEAD SPEED MP 2026 "})
        self.assertRedirects(response, reverse("club:shop_estimate_history"))
        self.assertEqual(ShopInquiry.objects.get().wanted_item, "HEAD SPEED MP 2026")
        response = self.client.post(reverse("club:shop_estimate"), {"wanted_item": " "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ShopInquiry.objects.count(), 1)

    def test_history_is_customer_scoped_and_coach_dashboard_is_protected(self):
        inquiry = create_inquiry(customer=self.other, wanted_item="他人の商品")
        self.client.force_login(self.customer)
        self.assertNotContains(self.client.get(reverse("club:shop_estimate_history")), inquiry.wanted_item)
        self.assertEqual(self.client.get(reverse("club:shop_coach")).status_code, 403)
        self.client.force_login(self.coach)
        self.assertContains(self.client.get(reverse("club:shop_coach")), inquiry.wanted_item)

    def make_quote(self, inquiry=None):
        return create_quote(customer=self.customer, creator=self.coach, inquiry=inquiry, note="",
            items=[{"description": "ラケット", "quantity": 1, "list_price": 44000, "sale_price": 35200},
                   {"description": "グリップ", "quantity": 3, "list_price": 400, "sale_price": 300}])

    def test_quote_calculations_number_expiry_and_inquiry_link(self):
        inquiry = create_inquiry(customer=self.customer, wanted_item="ラケット")
        quote = self.make_quote(inquiry)
        self.assertRegex(quote.quote_number, r"^EST-\d{6}-\d{4,}$")
        self.assertEqual(quote.valid_until, one_month_after(quote.quote_date))
        self.assertEqual((quote.list_total, quote.discount_total, quote.total), (45200, 9100, 36100))
        item = quote.items.first()
        self.assertEqual((item.discount_amount, item.discount_rate), (8800, 20.0))
        inquiry.refresh_from_db()
        self.assertEqual((inquiry.status, inquiry.quoted_amount), (ShopInquiry.STATUS_QUOTED, 36100))
        second = self.make_quote()
        self.assertNotEqual(quote.quote_number, second.quote_number)

    def test_zero_list_price_and_month_end_are_safe(self):
        quote = create_quote(customer=self.customer, creator=self.coach,
            items=[{"description": "試供品", "quantity": 1, "list_price": 0, "sale_price": 0}])
        self.assertIsNone(quote.items.get().discount_rate)
        self.assertEqual(one_month_after(date(2025, 1, 31)), date(2025, 2, 28))

    def test_purchase_request_is_not_sale_and_confirmation_is_idempotent(self):
        quote = self.make_quote()
        request_purchase(quote=quote, customer=self.customer)
        self.assertFalse(ShopPurchase.objects.exists())
        purchase, created = confirm_quote_purchase(quote=quote, actor=self.coach)
        duplicate, created_again = confirm_quote_purchase(quote=quote, actor=self.coach)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(purchase.pk, duplicate.pk)
        self.assertEqual(purchase.amount, 36100)

    def test_direct_purchase_appears_only_for_customer(self):
        purchase = create_direct_purchase(customer=self.customer, actor=self.coach,
            description="口頭注文", quantity=1, amount=5000)
        self.client.force_login(self.customer)
        self.assertContains(self.client.get(reverse("club:shop_estimate_history")), "口頭注文")
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(reverse("club:shop_estimate_history")), "口頭注文")

    def test_quote_permissions_purchase_request_and_pdf(self):
        quote = self.make_quote()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("club:shop_quote_detail", args=[quote.pk])).status_code, 404)
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(reverse("club:shop_quote_pdf", args=[quote.pk])).status_code, 200)
        self.client.post(reverse("club:shop_quote_purchase_request", args=[quote.pk]))
        quote.refresh_from_db()
        self.assertEqual(quote.status, ShopQuote.STATUS_PURCHASE_REQUESTED)
        pdf = build_quote_pdf(quote)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertIn("304A898B7A4D66F8", pdf.decode("latin1"))  # お見積書 UTF-16BE


class ShopAllocationTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user("allocation-customer")
        self.coaches = [User.objects.create_user(f"allocation-coach-{i}", role=User.ROLE_COACH) for i in range(3)]
        self.admin = User.objects.create_superuser("allocation-admin")
        self.purchase = create_direct_purchase(customer=self.customer, actor=self.coaches[0], description="商品", quantity=1, amount=36100)

    def test_admin_can_save_exact_and_partial_amount_allocations(self):
        summary = save_allocations(purchase=self.purchase, actor=self.admin,
            amounts={self.coaches[0].pk: 20000, self.coaches[1].pk: 10000, self.coaches[2].pk: 6100})
        self.assertEqual(summary, {"allocated": 36100, "remaining": 0, "complete": True})
        allocation = ShopRevenueAllocation.objects.get(purchase=self.purchase, coach=self.coaches[0])
        self.assertEqual(allocation.percentage, 55.4)
        summary = save_allocations(purchase=self.purchase, actor=self.admin,
            amounts={self.coaches[0].pk: 20000, self.coaches[1].pk: 10000, self.coaches[2].pk: 5000})
        self.assertEqual((summary["remaining"], summary["complete"]), (1100, False))
        self.assertEqual(self.purchase.allocation_audits.count(), 2)

    def test_over_negative_non_admin_and_canceled_are_rejected_or_excluded(self):
        with self.assertRaises(ValidationError):
            save_allocations(purchase=self.purchase, actor=self.admin, amounts={self.coaches[0].pk: 40000})
        with self.assertRaises(ValidationError):
            save_allocations(purchase=self.purchase, actor=self.admin, amounts={self.coaches[0].pk: -1})
        with self.assertRaises(PermissionError):
            save_allocations(purchase=self.purchase, actor=self.coaches[0], amounts={self.coaches[0].pk: 0})
        save_allocations(purchase=self.purchase, actor=self.admin, amounts={self.coaches[0].pk: 36100})
        self.purchase.amount = 35000
        self.purchase.save(update_fields=["amount"])
        self.assertFalse(allocation_summary(self.purchase)["complete"])
        self.purchase.status = ShopPurchase.STATUS_CANCELED
        self.purchase.save(update_fields=["status"])
        month = timezone.localdate()
        self.assertEqual(monthly_shop_allocations(month.year, month.month), {})

    def test_allocation_page_is_admin_only(self):
        self.client.force_login(self.coaches[0])
        self.assertEqual(self.client.get(reverse("club:shop_allocation", args=[self.purchase.pk])).status_code, 403)
