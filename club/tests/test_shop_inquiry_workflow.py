from datetime import date
from decimal import Decimal
from unittest.mock import patch
from io import BytesIO

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.models import (MAIN_COACH_NAMES, ShopInquiry, ShopPurchase, ShopQuote,
                         ShopRevenueAllocation, User)
from club.shop_pdf import build_quote_pdf
from club.shop_service import (allocation_summary, confirm_quote_purchase,
    create_direct_purchase, create_inquiry, create_quote, monthly_shop_allocations,
    one_month_after, request_purchase, save_allocations, sale_price_from_discount,
    discount_rate_from_prices, profit_summary, save_quote_accounting, update_quote)
from club.shop_forms import ShopQuoteForm, ShopQuoteItemForm
from pypdf import PdfReader


class ShopWorkflowTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user("shop-customer", password="pw", full_name="顧客 太郎")
        self.other = User.objects.create_user("shop-other", password="pw")
        self.coach = User.objects.create_user("shop-coach", password="pw", role=User.ROLE_COACH)
        self.admin = User.objects.create_superuser("shop-admin", password="pw")
        self.main_coaches = [User.objects.create_user(
            f"shop-main-{index}", role=User.ROLE_COACH, full_name=name
        ) for index, name in enumerate(MAIN_COACH_NAMES)]

    def test_customer_can_submit_free_text_and_empty_is_rejected(self):
        self.client.force_login(self.customer)
        response = self.client.post(reverse("club:shop_estimate"), {"wanted_item": " HEAD SPEED MP 2026 "})
        self.assertRedirects(response, reverse("club:shop_estimate_history"))
        self.assertEqual(ShopInquiry.objects.get().wanted_item, "HEAD SPEED MP 2026")
        response = self.client.post(reverse("club:shop_estimate"), {"wanted_item": " "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ShopInquiry.objects.count(), 1)

    @patch("club.notification_service.deliver")
    def test_inquiry_notification_is_after_commit_and_contains_details(self, deliver):
        self.admin.email = "admin@example.com"
        self.admin.save(update_fields=["email"])
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            inquiry = create_inquiry(customer=self.customer, wanted_item="HEAD SPEED MP")
        deliver.assert_not_called()
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        message = deliver.call_args.kwargs["message"]
        self.assertIn(self.customer.display_name(), message)
        self.assertIn(inquiry.wanted_item, message)
        self.assertIn(reverse("club:shop_coach"), message)

    @patch("club.notification_service.deliver")
    def test_failed_inquiry_transaction_sends_no_notification(self, deliver):
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(ValidationError):
                create_inquiry(customer=self.customer, wanted_item=" ")
        deliver.assert_not_called()

    def test_history_is_customer_scoped_and_coach_dashboard_is_protected(self):
        inquiry = create_inquiry(customer=self.other, wanted_item="他人の商品")
        self.client.force_login(self.customer)
        self.assertNotContains(self.client.get(reverse("club:shop_estimate_history")), inquiry.wanted_item)
        self.assertEqual(self.client.get(reverse("club:shop_coach")).status_code, 403)
        self.client.force_login(self.coach)
        self.assertContains(self.client.get(reverse("club:shop_coach")), inquiry.wanted_item)

    def make_quote(self, inquiry=None, configured=True):
        quote = create_quote(customer=self.customer, creator=self.coach, inquiry=inquiry, note="",
            items=[{"description": "ラケット", "quantity": 1, "list_price": 44000, "sale_price": 35200},
                   {"description": "グリップ", "quantity": 3, "list_price": 400, "sale_price": 300}])
        if configured:
            save_quote_accounting(quote=quote, actor=self.admin, sale_amount=36100,
                purchase_cost=30000, procurement_coach=self.main_coaches[0], amounts={
                    self.main_coaches[0].pk: 3100, self.main_coaches[1].pk: 2000,
                    self.main_coaches[2].pk: 1000})
        return quote

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

    def test_bidirectional_pricing_is_server_validated(self):
        self.assertEqual(sale_price_from_discount(44000, Decimal("20")), 35200)
        self.assertEqual(discount_rate_from_prices(44000, 35200), Decimal("20.0"))
        form = ShopQuoteItemForm({"description": "ラケット", "quantity": 1, "list_price": 44000,
                                  "sale_price": 1, "discount_rate": "20", "pricing_source": "discount"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["sale_price"], 35200)
        for rate in ("-1", "100.1"):
            invalid = ShopQuoteItemForm({"description": "x", "quantity": 1, "list_price": 44000,
                                         "sale_price": 0, "discount_rate": rate, "pricing_source": "discount"})
            self.assertFalse(invalid.is_valid())
        zero = ShopQuoteItemForm({"description": "x", "quantity": 1, "list_price": 0,
                                  "sale_price": 1, "pricing_source": "sale"})
        self.assertFalse(zero.is_valid())

    def test_cost_profit_totals_and_purchase_snapshot(self):
        quote = create_quote(customer=self.customer, creator=self.coach, items=[
            {"description": "ラケット", "quantity": 2, "list_price": 44000, "sale_price": 35200, "cost_price": 28000},
            {"description": "バッグ", "quantity": 1, "list_price": 10000, "sale_price": 8000, "cost_price": 5000},
        ])
        save_quote_accounting(quote=quote, actor=self.admin, sale_amount=78400,
            purchase_cost=61000, procurement_coach=self.main_coaches[0], amounts={
                self.main_coaches[0].pk: 10000, self.main_coaches[1].pk: 5000,
                self.main_coaches[2].pk: 2400})
        item = quote.items.first()
        self.assertEqual((item.unit_profit, item.profit_rate, item.line_profit), (7200, 20.5, 14400))
        self.assertEqual(profit_summary(quote.items.all()), {"revenue": 78400, "cost": 61000, "profit": 17400, "margin": Decimal("22.2")})
        request_purchase(quote=quote, customer=self.customer)
        purchase, _ = confirm_quote_purchase(quote=quote, actor=self.coach)
        self.assertEqual(purchase.cost_total, 61000)
        self.assertEqual(purchase.profit_amount_snapshot, 17400)
        self.assertEqual(purchase.allocations.count(), 3)

    def test_guest_quote_validation_snapshot_and_accounting_are_independent_of_buyer_type(self):
        form = ShopQuoteForm({"purchaser_type": "guest", "customer": self.customer.pk,
                              "guest_name": "  山田 太郎  ", "inquiry": "", "note": ""})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.cleaned_data["customer"])
        self.assertEqual(form.cleaned_data["guest_name"], "山田 太郎")
        quote = create_quote(customer=None, guest_name=form.cleaned_data["guest_name"],
            creator=self.coach, items=[{"description": "ラケット", "quantity": 1,
            "list_price": 36100, "sale_price": 36100, "cost_price": 30000}])
        save_quote_accounting(quote=quote, actor=self.admin, sale_amount=36100,
            purchase_cost=30000, procurement_coach=self.main_coaches[0], amounts={
                self.main_coaches[0].pk: 3100, self.main_coaches[1].pk: 2000,
                self.main_coaches[2].pk: 1000})
        purchase, created = confirm_quote_purchase(quote=quote, actor=self.coach)
        self.assertTrue(created)
        self.assertIsNone(purchase.customer)
        self.assertEqual((purchase.guest_name, purchase.purchaser_name), ("山田 太郎", "山田 太郎"))
        self.assertEqual((purchase.amount, purchase.cost_total, purchase.profit_amount_snapshot),
                         (36100, 30000, 6100))
        self.assertEqual(sum(purchase.allocations.values_list("amount", flat=True)), 6100)
        quote.guest_name = "変更後"
        quote.full_clean()
        quote.save(update_fields=["guest_name"])
        purchase.refresh_from_db()
        self.assertEqual(purchase.guest_name, "山田 太郎")

    def test_buyer_exclusivity_and_guest_form_require_name(self):
        for customer, guest_name in ((self.customer, "ゲスト"), (None, ""), (None, "   ")):
            quote = ShopQuote(quote_number="invalid", customer=customer, guest_name=guest_name,
                valid_until=timezone.localdate(), created_by=self.coach)
            with self.assertRaises(ValidationError):
                quote.full_clean()
        missing = ShopQuoteForm({"purchaser_type": "guest", "guest_name": "", "inquiry": ""})
        self.assertFalse(missing.is_valid())
        self.assertIn("guest_name", missing.errors)
        member = ShopQuoteForm({"purchaser_type": "member", "customer": self.customer.pk,
                                "guest_name": "消去される", "inquiry": ""})
        self.assertTrue(member.is_valid(), member.errors)
        self.assertEqual(member.cleaned_data["guest_name"], "")
        self.customer.full_name = "表示名 会員"
        self.customer.save(update_fields=["full_name"])
        self.assertIn("表示名 会員", str(ShopQuoteForm()["customer"]))
        self.assertNotIn("shop-customer", str(ShopQuoteForm()["customer"]))

    def test_guest_quote_can_be_created_and_edited_from_staff_ui(self):
        self.client.force_login(self.coach)
        data = {"purchaser_type": "guest", "customer": "", "guest_name": "  外部 花子  ",
                "inquiry": "", "note": "", "items-TOTAL_FORMS": "1",
                "items-INITIAL_FORMS": "0", "items-MIN_NUM_FORMS": "1",
                "items-MAX_NUM_FORMS": "1000", "items-0-description": "商品",
                "items-0-quantity": "1", "items-0-list_price": "1000",
                "items-0-sale_price": "1000", "items-0-cost_price": "500",
                "items-0-pricing_source": "sale"}
        response = self.client.post(reverse("club:shop_quote_create"), data)
        quote = ShopQuote.objects.get(guest_name="外部 花子")
        self.assertRedirects(response, reverse("club:shop_quote_detail", args=[quote.pk]))
        data.update({"guest_name": " 外部 花子 改 ", "items-INITIAL_FORMS": "1"})
        response = self.client.post(reverse("club:shop_quote_edit", args=[quote.pk]), data)
        self.assertRedirects(response, reverse("club:shop_quote_detail", args=[quote.pk]))
        quote.refresh_from_db()
        self.assertEqual((quote.customer_id, quote.guest_name), (None, "外部 花子 改"))

    def test_inquiry_quote_can_change_to_guest_with_items_and_accounting(self):
        inquiry = create_inquiry(customer=self.customer, wanted_item="問い合わせ商品")
        quote = self.make_quote(inquiry=inquiry)
        number, quote_date = quote.quote_number, quote.quote_date
        original_customer_id = inquiry.customer_id
        self.client.force_login(self.admin)
        data = {
            "purchaser_type": "guest", "customer": str(self.customer.pk),
            "guest_name": "  外部 顧客  ", "inquiry": str(inquiry.pk), "note": "更新",
            "accounting_sale_amount": "33825", "accounting_purchase_cost": "26053",
            "procurement_coach": str(self.main_coaches[0].pk),
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "1",
            "items-MIN_NUM_FORMS": "1", "items-MAX_NUM_FORMS": "1000",
            "items-0-description": "更新商品", "items-0-quantity": "1",
            "items-0-list_price": "33825", "items-0-sale_price": "33825",
            "items-0-cost_price": "26053", "items-0-pricing_source": "sale",
            f"accounting_coach_{self.main_coaches[0].pk}": "4372",
            f"accounting_coach_{self.main_coaches[1].pk}": "1200",
            f"accounting_coach_{self.main_coaches[2].pk}": "2200",
        }
        response = self.client.post(reverse("club:shop_quote_edit", args=[quote.pk]), data)
        self.assertRedirects(response, reverse("club:shop_quote_detail", args=[quote.pk]))
        quote.refresh_from_db()
        inquiry.refresh_from_db()
        self.assertEqual((quote.customer_id, quote.guest_name, quote.inquiry_id), (None, "外部 顧客", None))
        self.assertEqual((quote.quote_number, quote.quote_date), (number, quote_date))
        self.assertEqual((quote.items.get().description, quote.total), ("更新商品", 33825))
        self.assertEqual((quote.accounting_sale_amount, quote.accounting_purchase_cost), (33825, 26053))
        self.assertEqual(sum(quote.planned_profit_allocations.values()), 7772)
        self.assertTrue(ShopInquiry.objects.filter(pk=inquiry.pk).exists())
        self.assertEqual(inquiry.customer_id, original_customer_id)
        self.assertEqual(inquiry.status, ShopInquiry.STATUS_QUOTED)

        response = self._edit_post(quote, [{
            "description": "会員向け商品", "quantity": "1", "list_price": "1000",
            "sale_price": "1000", "cost_price": "500", "pricing_source": "sale",
        }], purchaser_type="member", customer=self.other.pk, guest_name="消去対象")
        self.assertRedirects(response, reverse("club:shop_quote_detail", args=[quote.pk]))
        quote.refresh_from_db()
        self.assertEqual((quote.customer_id, quote.guest_name, quote.inquiry_id), (self.other.pk, "", None))

    def test_invalid_hidden_inquiry_error_and_common_guidance_are_visible(self):
        self.client.force_login(self.coach)
        response = self.client.post(reverse("club:shop_quote_create"), {
            "purchaser_type": "member", "customer": str(self.customer.pk),
            "guest_name": "", "inquiry": "999999", "note": "",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "1", "items-MAX_NUM_FORMS": "1000",
            "items-0-description": "商品", "items-0-quantity": "1",
            "items-0-list_price": "1000", "items-0-sale_price": "1000",
            "items-0-cost_price": "500", "items-0-pricing_source": "sale",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "入力内容を確認してください。")
        self.assertContains(response, response.context["form"].errors["inquiry"][0])

    def test_guest_pdf_and_management_views_use_canonical_purchaser_name(self):
        quote = create_quote(customer=None, guest_name="外部 顧客", creator=self.coach,
            items=[{"description": "商品", "quantity": 1, "list_price": 1000,
                    "sale_price": 1000, "cost_price": 500}])
        pdf_text = "\n".join(page.extract_text() or "" for page in
                             PdfReader(BytesIO(build_quote_pdf(quote))).pages)
        self.assertIn("外部 顧客", pdf_text)
        for private in ("原価", "仕入額", "仕入コーチ", "利益", "利益率", "利益分配"):
            self.assertNotIn(private, pdf_text)
        self.client.force_login(self.coach)
        self.assertContains(self.client.get(reverse("club:shop_coach")), "外部 顧客")
        self.assertContains(self.client.get(reverse("club:shop_quote_detail", args=[quote.pk])), "外部 顧客")

    def test_quote_accounting_draft_snapshot_permissions_and_audit(self):
        quote = self.make_quote(configured=False)
        with self.assertRaises(PermissionError):
            save_quote_accounting(quote=quote, actor=self.coach, sale_amount=36100,
                purchase_cost=32100, procurement_coach=self.main_coaches[0], amounts={})
        summary = save_quote_accounting(quote=quote, actor=self.admin, sale_amount=36100,
            purchase_cost=32100, procurement_coach=self.main_coaches[0], amounts={
                self.main_coaches[0].pk: 2000, self.main_coaches[1].pk: 1000,
                self.main_coaches[2].pk: 500})
        self.assertEqual((summary["profit"], summary["remaining"], summary["complete"]), (4000, 500, False))
        self.assertEqual(quote.accounting_audits.count(), 1)
        with self.assertRaisesMessage(ValidationError, "残額: 500円"):
            confirm_quote_purchase(quote=quote, actor=self.coach)
        save_quote_accounting(quote=quote, actor=self.admin, sale_amount=36100,
            purchase_cost=32100, procurement_coach=self.main_coaches[0], amounts={
                self.main_coaches[0].pk: 2000, self.main_coaches[1].pk: 1000,
                self.main_coaches[2].pk: 1000})
        purchase, created = confirm_quote_purchase(quote=quote, actor=self.coach)
        self.assertTrue(created)
        original = (purchase.amount, purchase.cost_total, purchase.procurement_coach_id,
                    list(purchase.allocations.order_by("coach_id").values_list("coach_id", "amount")))
        quote.accounting_sale_amount = 99999
        quote.planned_profit_allocations = {}
        quote.save(update_fields=["accounting_sale_amount", "planned_profit_allocations"])
        purchase.refresh_from_db()
        self.assertEqual(original, (purchase.amount, purchase.cost_total, purchase.procurement_coach_id,
                    list(purchase.allocations.order_by("coach_id").values_list("coach_id", "amount"))))

    def test_unconfigured_quote_cannot_be_confirmed(self):
        quote = self.make_quote(configured=False)
        with self.assertRaisesMessage(ValidationError, "内部精算情報"):
            confirm_quote_purchase(quote=quote, actor=self.coach)

    def test_draft_allows_overallocation_and_zero_profit_confirms(self):
        quote = self.make_quote(configured=False)
        summary = save_quote_accounting(quote=quote, actor=self.admin, sale_amount=36100,
            purchase_cost=30000, procurement_coach=self.main_coaches[0], amounts={
                self.main_coaches[0].pk: 7000, self.main_coaches[1].pk: 0,
                self.main_coaches[2].pk: 0})
        self.assertEqual(summary["remaining"], -900)
        with self.assertRaisesMessage(ValidationError, "残額: -900円"):
            confirm_quote_purchase(quote=quote, actor=self.admin)
        summary = save_quote_accounting(quote=quote, actor=self.admin, sale_amount=36100,
            purchase_cost=36100, procurement_coach=self.main_coaches[0], amounts={
                coach.pk: 0 for coach in self.main_coaches})
        self.assertTrue(summary["complete"])
        purchase, _ = confirm_quote_purchase(quote=quote, actor=self.admin)
        self.assertEqual((purchase.profit_amount_snapshot, purchase.allocations.count()), (0, 3))

    def test_changed_quote_total_requires_accounting_review(self):
        quote = self.make_quote()
        quote.items.first().delete()
        with self.assertRaisesMessage(ValidationError, "見積合計"):
            confirm_quote_purchase(quote=quote, actor=self.coach)

    def test_missing_cost_is_not_treated_as_zero_and_customer_outputs_hide_profit(self):
        quote = self.make_quote()
        self.assertIsNone(profit_summary(quote.items.all())["profit"])
        self.client.force_login(self.customer)
        html = self.client.get(reverse("club:shop_quote_detail", args=[quote.pk])).content.decode()
        self.assertNotIn("原価", html)
        self.assertNotIn("利益率", html)
        pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(build_quote_pdf(quote))).pages)
        self.assertNotIn("原価", pdf_text)
        self.assertNotIn("利益", pdf_text)

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
        text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages)
        self.assertIn("お見積書", text)

    def test_purchase_request_actions_are_customer_only(self):
        quote = self.make_quote()
        url = reverse("club:shop_quote_detail", args=[quote.pk])
        action = reverse("club:shop_quote_purchase_request", args=[quote.pk])
        self.client.force_login(self.coach)
        self.assertNotContains(self.client.get(url), "この内容で購入を希望する")
        self.assertEqual(self.client.post(action).status_code, 404)
        quote.refresh_from_db()
        self.assertEqual(quote.status, ShopQuote.STATUS_SENT)
        self.client.force_login(self.customer)
        self.assertContains(self.client.get(url), "この内容で購入を希望する")
        self.assertRedirects(self.client.post(action), url)
        quote.refresh_from_db()
        self.assertEqual(quote.status, ShopQuote.STATUS_PURCHASE_REQUESTED)
        self.client.force_login(self.coach)
        self.assertContains(self.client.get(url), "購入を確定する")

    def test_quote_purchase_actions_are_separated_by_role(self):
        confirm_text = "購入を確定する"
        request_text = "この内容で購入を希望する"

        sent_quote = self.make_quote()
        detail = reverse("club:shop_quote_detail", args=[sent_quote.pk])
        confirm = reverse("club:shop_quote_confirm", args=[sent_quote.pk])

        self.client.force_login(self.customer)
        response = self.client.get(detail)
        self.assertContains(response, request_text)
        self.assertNotContains(response, confirm_text)
        self.assertEqual(self.client.post(confirm).status_code, 403)
        sent_quote.refresh_from_db()
        self.assertEqual(sent_quote.status, ShopQuote.STATUS_SENT)
        self.assertFalse(ShopPurchase.objects.filter(quote=sent_quote).exists())

        self.client.force_login(self.coach)
        response = self.client.get(detail)
        self.assertContains(response, confirm_text)
        self.assertNotContains(response, request_text)
        self.assertRedirects(self.client.post(confirm), reverse("club:shop_coach"))
        sent_quote.refresh_from_db()
        self.assertEqual(sent_quote.status, ShopQuote.STATUS_PURCHASED)
        self.assertEqual(ShopPurchase.objects.filter(quote=sent_quote).count(), 1)
        self.assertNotContains(self.client.get(detail), confirm_text)
        self.client.post(confirm)
        self.assertEqual(ShopPurchase.objects.filter(quote=sent_quote).count(), 1)

        for actor in (self.coach, self.admin):
            for requested in (False, True):
                quote = self.make_quote()
                if requested:
                    request_purchase(quote=quote, customer=self.customer)
                url = reverse("club:shop_quote_detail", args=[quote.pk])
                action = reverse("club:shop_quote_confirm", args=[quote.pk])
                self.client.force_login(actor)
                self.assertContains(self.client.get(url), confirm_text)
                self.assertRedirects(self.client.post(action), reverse("club:shop_coach"))
                self.assertEqual(ShopPurchase.objects.filter(quote=quote).count(), 1)

        canceled = self.make_quote()
        canceled.status = ShopQuote.STATUS_CANCELED
        canceled.save(update_fields=["status"])
        self.client.force_login(self.admin)
        canceled_detail = reverse("club:shop_quote_detail", args=[canceled.pk])
        canceled_confirm = reverse("club:shop_quote_confirm", args=[canceled.pk])
        self.assertNotContains(self.client.get(canceled_detail), confirm_text)
        self.assertRedirects(self.client.post(canceled_confirm), canceled_detail)
        self.assertFalse(ShopPurchase.objects.filter(quote=canceled).exists())

    def test_other_customer_cannot_request_purchase(self):
        quote = self.make_quote()
        self.client.force_login(self.other)
        response = self.client.post(reverse("club:shop_quote_purchase_request", args=[quote.pk]))
        self.assertEqual(response.status_code, 404)
        quote.refresh_from_db()
        self.assertEqual(quote.status, ShopQuote.STATUS_SENT)

    def test_customer_pdf_contains_public_japanese_fields_only(self):
        quote = create_quote(customer=self.customer, creator=self.coach, note="ご検討ください", items=[
            {"description": "HEAD SPEED MP", "quantity": 1, "list_price": 44000,
             "sale_price": 32600, "cost_price": 25000},
            {"description": "グリップテープ", "quantity": 2, "list_price": 1000,
             "sale_price": 800, "cost_price": 400},
        ])
        raw = build_quote_pdf(quote)
        reader = PdfReader(BytesIO(raw))
        source = "\n".join(page.extract_text() or "" for page in reader.pages)
        for value in ("Play Design Tennis", "お見積書", "見積番号", quote.quote_number,
                      "見積日", "有効期限", "お客様名", self.customer.display_name(),
                      "商品名・内容", "HEAD SPEED MP", "グリップテープ", "数量",
                      "定価", "値引き", "販売価格", "明細金額", "定価合計",
                      "お値引き", "お見積合計", "備考"):
            self.assertIn(str(value), source)
        for private in ("原価", "利益", "利益率", "内部利益集計"):
            self.assertNotIn(private, source)
        self.assertEqual(tuple(round(float(value)) for value in reader.pages[0].mediabox[2:]), (595, 842))
        fonts = reader.pages[0]["/Resources"]["/Font"]
        embedded = False
        for font_ref in fonts.values():
            font = font_ref.get_object()
            descriptor = font.get("/FontDescriptor")
            if descriptor:
                descriptor = descriptor.get_object()
                embedded = any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3"))
            descendants = font.get("/DescendantFonts", [])
            for descendant in descendants:
                descriptor = descendant.get_object().get("/FontDescriptor")
                if descriptor:
                    descriptor = descriptor.get_object()
                    embedded = embedded or any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3"))
        self.assertTrue(embedded, "Japanese font must be embedded in the PDF")

    def test_customer_pdf_wraps_long_japanese_product_names_and_multiple_items(self):
        long_name = "長い日本語商品名" * 12
        quote = create_quote(customer=self.customer, creator=self.coach, note="日本語の備考です", items=[
            {"description": f"{long_name}{index}", "quantity": 1, "list_price": 1000,
             "sale_price": 900} for index in range(12)
        ])
        reader = PdfReader(BytesIO(build_quote_pdf(quote)))
        self.assertGreaterEqual(len(reader.pages), 1)
        self.assertEqual(tuple(round(float(value)) for value in reader.pages[0].mediabox[2:]), (595, 842))
        source = "\n".join(page.extract_text() or "" for page in reader.pages)
        self.assertIn(long_name, "".join(source.split()))
        self.assertIn("日本語の備考です", source)

    def _edit_post(self, quote, rows, **form_overrides):
        data = {
            "purchaser_type": form_overrides.get("purchaser_type", "member"),
            "customer": str(form_overrides.get("customer", self.customer.pk)),
            "guest_name": form_overrides.get("guest_name", ""),
            "inquiry": "", "note": form_overrides.get("note", "更新備考"),
            "items-TOTAL_FORMS": str(len(rows)), "items-INITIAL_FORMS": str(min(2, len(rows))),
            "items-MIN_NUM_FORMS": "1", "items-MAX_NUM_FORMS": "1000",
        }
        for index, row in enumerate(rows):
            for key, value in row.items():
                data[f"items-{index}-{key}"] = value
        return self.client.post(reverse("club:shop_quote_edit", args=[quote.pk]), data)

    def test_quote_edit_permissions_fields_and_number_are_preserved(self):
        quote = self.make_quote()
        number, quote_date = quote.quote_number, quote.quote_date
        detail = reverse("club:shop_quote_detail", args=[quote.pk])
        self.client.force_login(self.customer)
        self.assertNotContains(self.client.get(detail), "見積を編集")
        self.assertEqual(self.client.get(reverse("club:shop_quote_edit", args=[quote.pk])).status_code, 403)
        self.client.force_login(self.coach)
        self.assertContains(self.client.get(detail), "見積を編集")
        edit_page = self.client.get(reverse("club:shop_quote_edit", args=[quote.pk]))
        self.assertContains(edit_page, "HEAD", count=0)
        self.assertContains(edit_page, "items-0-discount_rate")
        self.assertContains(edit_page, "data-unit-profit")
        response = self._edit_post(quote, [
            {"description": "HEAD SPEED MP", "quantity": "2", "list_price": "44000",
             "sale_price": "1", "discount_rate": "25", "cost_price": "25000", "pricing_source": "discount"},
            {"description": "グリップ", "quantity": "3", "list_price": "1000",
             "sale_price": "700", "discount_rate": "30", "cost_price": "400", "pricing_source": "sale"},
            {"description": "バッグ", "quantity": "1", "list_price": "10000",
             "sale_price": "8000", "discount_rate": "20", "cost_price": "5000", "pricing_source": "sale"},
        ])
        self.assertRedirects(response, detail)
        quote.refresh_from_db()
        self.assertEqual((quote.quote_number, quote.quote_date), (number, quote_date))
        self.assertEqual(quote.items.count(), 3)
        first = quote.items.first()
        self.assertEqual((first.description, first.quantity, first.sale_price, first.cost_price),
                         ("HEAD SPEED MP", 2, 33000, 25000))
        self.assertEqual(first.line_profit, 16000)

    def test_editing_requested_quote_requires_customer_reconfirmation(self):
        quote = self.make_quote()
        request_purchase(quote=quote, customer=self.customer)
        self.client.force_login(self.coach)
        response = self._edit_post(quote, [
            {"description": "再提示商品", "quantity": "1", "list_price": "50000",
             "sale_price": "40000", "discount_rate": "20", "cost_price": "30000", "pricing_source": "sale"},
        ])
        self.assertEqual(response.status_code, 302)
        quote.refresh_from_db()
        self.assertEqual(quote.status, ShopQuote.STATUS_SENT)
        self.client.force_login(self.customer)
        self.assertContains(self.client.get(reverse("club:shop_quote_detail", args=[quote.pk])),
                            "この内容で購入を希望する")
        self.client.post(reverse("club:shop_quote_purchase_request", args=[quote.pk]))
        quote.refresh_from_db()
        self.assertEqual(quote.status, ShopQuote.STATUS_PURCHASE_REQUESTED)

    def test_purchased_and_canceled_quotes_cannot_be_edited(self):
        for status in (ShopQuote.STATUS_PURCHASED, ShopQuote.STATUS_CANCELED):
            quote = self.make_quote()
            quote.status = status
            quote.save(update_fields=["status"])
            self.client.force_login(self.coach)
            response = self.client.get(reverse("club:shop_quote_edit", args=[quote.pk]))
            self.assertRedirects(response, reverse("club:shop_quote_detail", args=[quote.pk]))
            with self.assertRaises(ValidationError):
                update_quote(quote=quote, customer=self.customer, note="", items=[{
                    "description": "x", "quantity": 1, "list_price": 1, "sale_price": 1,
                    "cost_price": 1,
                }])


class ShopAllocationTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user("allocation-customer")
        self.coaches = [User.objects.create_user(f"allocation-coach-{i}", role=User.ROLE_COACH, full_name=name) for i, name in enumerate(MAIN_COACH_NAMES)]
        self.admin = User.objects.create_superuser("allocation-admin")
        self.purchase = create_direct_purchase(customer=self.customer, actor=self.coaches[0], description="商品", quantity=1, amount=36100)

    def test_admin_can_save_exact_and_partial_amount_allocations(self):
        summary = save_allocations(purchase=self.purchase, actor=self.admin,
            amounts={self.coaches[0].pk: 6100, self.coaches[1].pk: 0, self.coaches[2].pk: 0},
            purchase_cost=30000, procurement_coach=self.coaches[0])
        self.assertEqual((summary["allocated"], summary["remaining"], summary["complete"]), (6100, 0, True))
        allocation = ShopRevenueAllocation.objects.get(purchase=self.purchase, coach=self.coaches[0])
        self.assertEqual(allocation.amount, 6100)
        audit = self.purchase.allocation_audits.get()
        self.assertEqual(audit.previous_snapshot["sale_amount"], 36100)
        self.assertIsNone(audit.previous_snapshot["purchase_cost"])
        self.assertEqual(audit.allocation_snapshot["purchase_cost"], 30000)
        with self.assertRaises(ValidationError):
            save_allocations(purchase=self.purchase, actor=self.admin,
                amounts={self.coaches[0].pk: 5000, self.coaches[1].pk: 0, self.coaches[2].pk: 0})
        self.assertEqual(self.purchase.allocation_audits.count(), 1)

    def test_over_negative_non_admin_and_canceled_are_rejected_or_excluded(self):
        with self.assertRaises(ValidationError):
            save_allocations(purchase=self.purchase, actor=self.admin, amounts={self.coaches[0].pk: 40000}, purchase_cost=0, procurement_coach=self.coaches[0])
        with self.assertRaises(ValidationError):
            save_allocations(purchase=self.purchase, actor=self.admin, amounts={self.coaches[0].pk: -1}, purchase_cost=36101, procurement_coach=self.coaches[0])
        with self.assertRaises(PermissionError):
            save_allocations(purchase=self.purchase, actor=self.coaches[0], amounts={self.coaches[0].pk: 0})
        save_allocations(purchase=self.purchase, actor=self.admin,
            amounts={self.coaches[0].pk: 6100, self.coaches[1].pk: 0, self.coaches[2].pk: 0},
            purchase_cost=30000, procurement_coach=self.coaches[0])
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
