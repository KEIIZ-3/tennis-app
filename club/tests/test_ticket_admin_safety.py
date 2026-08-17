from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import RequestFactory, TestCase
from django.urls import reverse

from club.admin import TicketConsumptionAdmin, TicketLedgerAdmin, TicketPurchaseAdmin
from club.models import TicketConsumption, TicketLedger, TicketPurchase


class TicketAdminSafetyTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.superuser = user_model.objects.create_superuser(
            username="ticket-admin",
            email="admin@example.com",
            password="password",
        )
        self.member = user_model.objects.create_user(
            username="ticket-member",
            password="password",
            ticket_balance=4,
        )
        self.purchase = TicketPurchase.objects.create(
            user=self.member,
            purchase_type=TicketPurchase.PURCHASE_TYPE_SET4,
            total_tickets=4,
            remaining_tickets=3,
            unit_price=3500,
        )
        self.consumption = TicketConsumption.objects.create(
            user=self.member,
            purchase=self.purchase,
            tickets_used=1,
            unit_price_snapshot=3500,
        )
        self.ledger = TicketLedger.objects.create(
            user=self.member,
            change_amount=4,
            balance_after=4,
            reason=TicketLedger.REASON_PURCHASE_SET4,
            created_by=self.superuser,
        )
        self.model_cases = (
            (TicketPurchase, TicketPurchaseAdmin, self.purchase),
            (TicketConsumption, TicketConsumptionAdmin, self.consumption),
            (TicketLedger, TicketLedgerAdmin, self.ledger),
        )

    def _request_for(self, user):
        request = RequestFactory().get("/")
        request.user = user
        return request

    def test_ticket_admin_crud_is_disabled_for_superuser(self):
        request = self._request_for(self.superuser)
        for model, admin_class, obj in self.model_cases:
            with self.subTest(model=model.__name__):
                model_admin = admin_class(model, admin.site)
                self.assertFalse(model_admin.has_add_permission(request))
                self.assertFalse(model_admin.has_change_permission(request, obj))
                self.assertFalse(model_admin.has_delete_permission(request, obj))

    def test_ticket_admin_is_read_only_for_staff_with_all_model_permissions(self):
        user_model = get_user_model()
        staff = user_model.objects.create_user(
            username="ticket-staff",
            password="password",
            is_staff=True,
        )
        codenames = []
        for model, _admin_class, _obj in self.model_cases:
            model_name = model._meta.model_name
            codenames.extend(
                f"{action}_{model_name}"
                for action in ("add", "change", "delete", "view")
            )
        staff.user_permissions.add(*Permission.objects.filter(codename__in=codenames))

        request = self._request_for(staff)
        for model, admin_class, obj in self.model_cases:
            with self.subTest(model=model.__name__):
                model_admin = admin_class(model, admin.site)
                self.assertFalse(model_admin.has_add_permission(request))
                self.assertFalse(model_admin.has_change_permission(request, obj))
                self.assertFalse(model_admin.has_delete_permission(request, obj))

    def test_ticket_admin_audit_pages_render_without_mutation_controls(self):
        self.client.force_login(self.superuser)
        for model, _admin_class, obj in self.model_cases:
            model_name = model._meta.model_name
            with self.subTest(model=model.__name__):
                changelist_url = reverse(f"admin:club_{model_name}_changelist")
                change_url = reverse(f"admin:club_{model_name}_change", args=[obj.pk])
                add_url = reverse(f"admin:club_{model_name}_add")
                delete_url = reverse(f"admin:club_{model_name}_delete", args=[obj.pk])

                changelist_response = self.client.get(changelist_url)
                self.assertEqual(changelist_response.status_code, 200)
                self.assertNotContains(changelist_response, add_url)
                action_form = changelist_response.context["action_form"]
                action_values = (
                    {value for value, _label in action_form.fields["action"].choices}
                    if action_form is not None
                    else set()
                )
                self.assertNotIn("delete_selected", action_values)

                change_response = self.client.get(change_url)
                self.assertEqual(change_response.status_code, 200)
                self.assertNotContains(change_response, 'name="_save"')
                self.assertNotContains(change_response, delete_url)
                self.assertEqual(self.client.get(add_url).status_code, 403)
                self.assertEqual(self.client.get(delete_url).status_code, 403)
                self.assertEqual(
                    self.client.post(delete_url, {"post": "yes"}).status_code,
                    403,
                )
                self.assertTrue(model.objects.filter(pk=obj.pk).exists())

    def test_direct_consumption_save_does_not_apply_canonical_side_effects(self):
        consumption = TicketConsumption.objects.create(
            user=self.member,
            purchase=self.purchase,
            tickets_used=2,
            unit_price_snapshot=9999,
        )

        self.purchase.refresh_from_db()
        self.member.refresh_from_db()
        self.assertEqual(self.purchase.remaining_tickets, 3)
        self.assertEqual(self.member.ticket_balance, 4)
        self.assertFalse(
            TicketLedger.objects.filter(
                user=self.member,
                change_amount=-2,
            ).exists()
        )
        self.assertIsNone(consumption.refunded_at)

    def test_direct_ledger_save_does_not_change_ticket_balance(self):
        TicketLedger.objects.create(
            user=self.member,
            change_amount=-99,
            balance_after=-95,
            reason=TicketLedger.REASON_ADMIN_ADJUST,
        )

        self.member.refresh_from_db()
        self.assertEqual(self.member.ticket_balance, 4)

    def test_grant_form_repeated_post_creates_one_purchase_and_ledger(self):
        self.client.force_login(self.superuser)
        url = reverse("admin:club_user_grant_tickets")
        response = self.client.get(url, {"ids": str(self.member.pk)})
        token = str(response.context["form"]["idempotency_token"].value())
        data = {
            "ids": str(self.member.pk),
            "idempotency_token": token,
            "grant_kind": "paid",
            "tickets": 4,
            "unit_price": 3500,
            "label": "4枚セット",
            "note": "現金購入",
        }

        first = self.client.post(url, data)
        second = self.client.post(url, data)

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.member.refresh_from_db()
        self.assertEqual(self.member.ticket_balance, 8)
        self.assertEqual(TicketPurchase.objects.filter(user=self.member).count(), 2)
        self.assertEqual(
            TicketLedger.objects.filter(user=self.member, change_amount=4).count(),
            2,
        )

    def test_grant_form_different_tokens_create_distinct_purchases(self):
        self.client.force_login(self.superuser)
        url = reverse("admin:club_user_grant_tickets")
        base = {
            "ids": str(self.member.pk),
            "grant_kind": "paid",
            "tickets": 1,
            "unit_price": 4000,
            "label": "1枚券",
            "note": "現金購入",
        }
        self.client.post(url, {**base, "idempotency_token": "11111111-1111-4111-8111-111111111111"})
        self.client.post(url, {**base, "idempotency_token": "22222222-2222-4222-8222-222222222222"})

        self.member.refresh_from_db()
        self.assertEqual(self.member.ticket_balance, 6)
        self.assertEqual(TicketPurchase.objects.filter(user=self.member).count(), 3)

    def test_repeated_admin_action_post_is_idempotent(self):
        self.client.force_login(self.superuser)
        url = reverse("admin:club_user_changelist")
        data = {
            "action": "grant_set4_tickets",
            "_selected_action": str(self.member.pk),
            "idempotency_token": "33333333-3333-4333-8333-333333333333",
            "index": "0",
        }

        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.member.refresh_from_db()
        self.assertEqual(self.member.ticket_balance, 8)
        self.assertEqual(TicketPurchase.objects.filter(user=self.member).count(), 2)
        self.assertEqual(TicketLedger.objects.filter(user=self.member).count(), 2)
