from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.models import TicketLedger, TicketPurchase, TicketPurchaseReservation, User
from club.ticket_purchase_reservation_service import (
    approve_purchase_reservation,
    cancel_purchase_reservation,
    create_purchase_reservation,
    ticket_expiration_from,
)


class TicketPurchaseReservationServiceTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user(username="ticket-member", role=User.ROLE_MEMBER, ticket_balance=0)
        self.other = User.objects.create_user(username="other-member", role=User.ROLE_MEMBER)
        self.main_coach = User.objects.create_user(username="main-coach", role=User.ROLE_COACH)
        self.contractor = User.objects.create_user(username="contractor", role=User.ROLE_CONTRACTOR_COACH)

    def test_single_and_set_reservations_snapshot_prices_without_granting(self):
        single = create_purchase_reservation(user=self.member, product_code="single")
        set4 = create_purchase_reservation(user=self.member, product_code="set4")
        self.member.refresh_from_db()
        self.assertEqual((single.ticket_count, single.unit_price, single.total_amount), (1, 4000, 4000))
        self.assertEqual((set4.ticket_count, set4.unit_price, set4.total_amount), (4, 3500, 14000))
        self.assertEqual(self.member.ticket_balance, 0)
        self.assertFalse(TicketPurchase.objects.exists())
        self.assertFalse(TicketLedger.objects.exists())

    def test_approval_uses_formal_purchase_path_and_is_idempotent(self):
        pending = create_purchase_reservation(user=self.member, product_code="set4")
        approved, created = approve_purchase_reservation(reservation_id=pending.pk, coach=self.main_coach)
        repeated, repeated_created = approve_purchase_reservation(reservation_id=pending.pk, coach=self.main_coach)
        self.member.refresh_from_db()
        self.assertTrue(created)
        self.assertFalse(repeated_created)
        self.assertEqual(approved.pk, repeated.pk)
        self.assertEqual(self.member.ticket_balance, 4)
        self.assertEqual(TicketPurchase.objects.count(), 1)
        purchase = TicketPurchase.objects.get()
        self.assertEqual((purchase.total_tickets, purchase.unit_price), (4, 3500))
        self.assertEqual(purchase.created_by, self.main_coach)
        self.assertAlmostEqual(purchase.expires_at, ticket_expiration_from(purchase.purchased_at), delta=timedelta(seconds=1))
        pending.refresh_from_db()
        self.assertEqual(pending.status, TicketPurchaseReservation.STATUS_APPROVED)
        self.assertEqual(pending.ticket_purchase, purchase)
        self.assertEqual(pending.approved_by, self.main_coach)
        self.assertIsNotNone(pending.approved_at)

    def test_contractor_cannot_approve(self):
        pending = create_purchase_reservation(user=self.member, product_code="single")
        with self.assertRaises(PermissionDenied):
            approve_purchase_reservation(reservation_id=pending.pk, coach=self.contractor)
        self.assertFalse(TicketPurchase.objects.exists())

    def test_canceled_reservation_cannot_be_approved(self):
        pending = create_purchase_reservation(user=self.member, product_code="single")
        cancel_purchase_reservation(reservation_id=pending.pk, user=self.member)
        with self.assertRaises(ValidationError):
            approve_purchase_reservation(reservation_id=pending.pk, coach=self.main_coach)
        self.member.refresh_from_db()
        self.assertEqual(self.member.ticket_balance, 0)

    def test_user_cannot_cancel_another_users_reservation(self):
        pending = create_purchase_reservation(user=self.member, product_code="single")
        with self.assertRaises(TicketPurchaseReservation.DoesNotExist):
            cancel_purchase_reservation(reservation_id=pending.pk, user=self.other)

    def test_customer_create_endpoint_does_not_create_formal_purchase(self):
        self.client.force_login(self.member)
        response = self.client.post(reverse("club:ticket_purchase_reservation_create"), {"product": "single"})
        self.assertRedirects(response, reverse("club:tickets"), fetch_redirect_response=False)
        self.assertEqual(TicketPurchaseReservation.objects.filter(user=self.member).count(), 1)
        self.assertFalse(TicketPurchase.objects.exists())

    def test_other_customer_cannot_cancel_via_direct_post(self):
        pending = create_purchase_reservation(user=self.member, product_code="single")
        self.client.force_login(self.other)
        response = self.client.post(reverse("club:ticket_purchase_reservation_cancel", args=[pending.pk]))
        self.assertEqual(response.status_code, 403)
        pending.refresh_from_db()
        self.assertEqual(pending.status, TicketPurchaseReservation.STATUS_PENDING)

    def test_contractor_cannot_open_or_post_approval_urls(self):
        pending = create_purchase_reservation(user=self.member, product_code="single")
        self.client.force_login(self.contractor)
        self.assertEqual(self.client.get(reverse("club:ticket_purchase_confirm")).status_code, 403)
        self.assertEqual(self.client.post(reverse("club:ticket_purchase_approve", args=[pending.pk])).status_code, 403)
        self.assertFalse(TicketPurchase.objects.exists())
