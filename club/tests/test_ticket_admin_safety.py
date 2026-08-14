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
