from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from club.admin import ReservationAdmin, ReservationAdminForm
from club.models import (
    CoachAvailability,
    Court,
    Reservation,
    ReservationParticipant,
    TicketConsumption,
    TicketPurchase,
)


class ReservationAdminStatusTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.admin_user = user_model.objects.create_superuser(
            username="reservation-admin",
            email="admin@example.com",
            password="password",
        )
        self.member = user_model.objects.create_user(
            username="reservation-member",
            email="member@example.com",
            password="password",
            role=user_model.ROLE_MEMBER,
            member_level=user_model.LEVEL_BEGINNER,
            is_profile_completed=True,
            ticket_balance=2,
        )
        self.coach = user_model.objects.create_user(
            username="reservation-coach",
            password="password",
            role=user_model.ROLE_COACH,
        )
        self.court = Court.objects.create(name="Reservation Admin Court")
        start_at = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=7), time(10))
        )
        self.availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=user_model.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=start_at + timedelta(hours=2),
            capacity=6,
        )
        self.reservation = Reservation.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court,
            availability=self.availability,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=user_model.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=start_at + timedelta(hours=2),
            status=Reservation.STATUS_ACTIVE,
        )
        self.purchase = TicketPurchase.objects.create(
            user=self.member,
            purchase_type=TicketPurchase.PURCHASE_TYPE_SINGLE,
            unit_price=2000,
            total_tickets=2,
            remaining_tickets=2,
        )
        self.reservation.consume_tickets(created_by=self.admin_user)
        ReservationParticipant.objects.create(
            reservation=self.reservation,
            parent=self.member,
            participant_type="self",
            participant_name="Reservation Member",
            participant_level=user_model.LEVEL_BEGINNER,
        )
        self.model_admin = ReservationAdmin(Reservation, admin.site)
        self.request = RequestFactory().post("/")
        self.request.user = self.admin_user

    def _form_data(self, target_status):
        reservation = Reservation.objects.get(pk=self.reservation.pk)
        data = {
            field.name: (
                getattr(reservation, field.attname)
                if getattr(reservation, field.attname) is not None
                else ""
            )
            for field in Reservation._meta.fields
            if field.editable and not field.auto_created
        }
        data.update(
            start_at=timezone.localtime(reservation.start_at).strftime("%Y-%m-%d %H:%M:%S"),
            end_at=timezone.localtime(reservation.end_at).strftime("%Y-%m-%d %H:%M:%S"),
            status=target_status,
            _save="Save",
        )
        return data

    def _bound_form(self, target_status):
        return ReservationAdminForm(
            data=self._form_data(target_status),
            instance=Reservation.objects.get(pk=self.reservation.pk),
        )

    @patch("club.notification_service.send_email_to_address")
    def test_admin_active_to_canceled_uses_canonical_cancel(self, notify_mock):
        form = self._bound_form(Reservation.STATUS_CANCELED)
        self.assertTrue(form.is_valid(), form.errors)
        obj = form.save(commit=False)

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            self.model_admin.save_model(self.request, obj, form, change=True)

        self.reservation.refresh_from_db()
        self.member.refresh_from_db()
        self.purchase.refresh_from_db()
        consumption = TicketConsumption.objects.get(reservation=self.reservation)
        participant = ReservationParticipant.objects.get(reservation=self.reservation)
        self.assertEqual(self.reservation.status, Reservation.STATUS_CANCELED)
        self.assertIsNotNone(self.reservation.ticket_refunded_at)
        self.assertIsNotNone(consumption.refunded_at)
        self.assertEqual(self.member.ticket_balance, 2)
        self.assertEqual(self.purchase.remaining_tickets, 2)
        self.assertEqual(self.reservation.participant_ticket_price_snapshot, 2000)
        self.assertEqual(participant.participant_name, "Reservation Member")
        self.assertEqual(len(callbacks), 1)
        notify_mock.assert_called_once()

    def test_admin_canceled_to_active_is_rejected(self):
        self.reservation.cancel(created_by=self.admin_user)
        form = self._bound_form(Reservation.STATUS_ACTIVE)
        self.assertFalse(form.is_valid())
        self.assertIn("正規の業務処理がない", form.errors["status"][0])

    def test_admin_canceled_to_pending_is_rejected(self):
        self.reservation.cancel(created_by=self.admin_user)
        form = self._bound_form(Reservation.STATUS_PENDING)
        self.assertFalse(form.is_valid())
        self.assertIn("正規の業務処理がない", form.errors["status"][0])

    def test_admin_active_to_pending_is_rejected(self):
        form = self._bound_form(Reservation.STATUS_PENDING)
        self.assertFalse(form.is_valid())

    def test_direct_status_save_only_triggers_signal_and_does_not_refund(self):
        self.reservation.status = Reservation.STATUS_CANCELED
        self.reservation.save(update_fields=["status"])
        self.reservation.refresh_from_db()
        self.purchase.refresh_from_db()
        consumption = TicketConsumption.objects.get(reservation=self.reservation)
        self.assertIsNone(self.reservation.ticket_refunded_at)
        self.assertIsNone(consumption.refunded_at)
        self.assertEqual(self.purchase.remaining_tickets, 1)

    def test_admin_change_views_render_and_invalid_post_preserves_form(self):
        self.client.force_login(self.admin_user)
        url = reverse("admin:club_reservation_change", args=[self.reservation.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url, {"status": Reservation.STATUS_PENDING})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "この状態変更には正規の業務処理がない")
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.STATUS_ACTIVE)

    @patch("club.models.Reservation.refund_tickets", side_effect=RuntimeError("refund failed"))
    def test_admin_cancel_rolls_back_status_when_refund_fails(self, _refund):
        form = self._bound_form(Reservation.STATUS_CANCELED)
        self.assertTrue(form.is_valid(), form.errors)
        with self.assertRaises(RuntimeError):
            self.model_admin.save_model(
                self.request,
                form.save(commit=False),
                form,
                change=True,
            )
        self.reservation.refresh_from_db()
        self.assertEqual(self.reservation.status, Reservation.STATUS_ACTIVE)

    def test_canonical_cancel_is_idempotent(self):
        self.assertTrue(self.reservation.cancel(created_by=self.admin_user))
        self.assertFalse(self.reservation.cancel(created_by=self.admin_user))
        self.assertEqual(
            TicketConsumption.objects.filter(
                reservation=self.reservation,
                refunded_at__isnull=False,
            ).count(),
            1,
        )

    def test_admin_physical_delete_is_disabled_for_superuser(self):
        self.assertFalse(self.model_admin.has_delete_permission(self.request))
        self.assertFalse(
            self.model_admin.has_delete_permission(self.request, self.reservation)
        )

        self.client.force_login(self.admin_user)
        change_url = reverse("admin:club_reservation_change", args=[self.reservation.pk])
        delete_url = reverse("admin:club_reservation_delete", args=[self.reservation.pk])

        change_response = self.client.get(change_url)
        self.assertEqual(change_response.status_code, 200)
        self.assertNotContains(change_response, delete_url)
        self.assertEqual(self.client.get(delete_url).status_code, 403)
        self.assertEqual(self.client.post(delete_url, {"post": "yes"}).status_code, 403)
        self.assertTrue(Reservation.objects.filter(pk=self.reservation.pk).exists())

    def test_admin_bulk_delete_action_is_not_available(self):
        self.client.force_login(self.admin_user)
        changelist_url = reverse("admin:club_reservation_changelist")
        response = self.client.get(changelist_url)

        self.assertEqual(response.status_code, 200)
        action_form = response.context["action_form"]
        action_values = (
            {
                value
                for value, _label in action_form.fields["action"].choices
            }
            if action_form is not None
            else set()
        )
        self.assertNotIn("delete_selected", action_values)
        post_response = self.client.post(
            changelist_url,
            {"action": "delete_selected", "_selected_action": [self.reservation.pk]},
        )
        self.assertEqual(post_response.status_code, 200)
        self.assertTrue(Reservation.objects.filter(pk=self.reservation.pk).exists())

    def test_admin_physical_delete_is_disabled_for_staff_with_delete_permission(self):
        user_model = get_user_model()
        staff = user_model.objects.create_user(
            username="reservation-delete-staff",
            password="password",
            is_staff=True,
        )
        staff.user_permissions.add(
            Permission.objects.get(codename="view_reservation"),
            Permission.objects.get(codename="change_reservation"),
            Permission.objects.get(codename="delete_reservation"),
        )
        request = RequestFactory().get("/")
        request.user = staff

        self.assertFalse(self.model_admin.has_delete_permission(request, self.reservation))
        self.client.force_login(staff)
        delete_url = reverse("admin:club_reservation_delete", args=[self.reservation.pk])
        self.assertEqual(self.client.get(delete_url).status_code, 403)
        self.assertTrue(Reservation.objects.filter(pk=self.reservation.pk).exists())
