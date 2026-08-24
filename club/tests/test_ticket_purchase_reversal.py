from datetime import datetime, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.models import CoachAvailability, Court, Reservation, TicketPurchaseReservation, User
from club.ticket_purchase_reservation_service import approve_purchase_reservation, create_purchase_reservation


class TicketPurchaseReversalViewTests(TestCase):
    def setUp(self):
        self.main_coach = User.objects.create_user(username="reversal-main", role=User.ROLE_COACH)
        self.contractor = User.objects.create_user(username="reversal-contractor", role=User.ROLE_CONTRACTOR_COACH)
        self.member = User.objects.create_user(username="reversal-member", role=User.ROLE_MEMBER, full_name="取消テスト会員")
        court = Court.objects.create(name="取消テストコート")
        start_at = timezone.make_aware(datetime.combine(timezone.localdate(), datetime.min.time()).replace(hour=10))
        self.availability = CoachAvailability.objects.create(
            coach=self.main_coach, coach_2=self.contractor, court=court,
            lesson_type=Reservation.LESSON_GENERAL, start_at=start_at,
            end_at=start_at + timedelta(hours=2), capacity=6,
        )
        self.lesson_reservation = Reservation.objects.create(
            user=self.member, coach=self.main_coach, court=court,
            availability=self.availability, lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER, start_at=start_at,
            end_at=start_at + timedelta(hours=2), status=Reservation.STATUS_ACTIVE,
        )
        self.purchase_reservation = create_purchase_reservation(user=self.member, product_code="set4")
        approve_purchase_reservation(
            reservation_id=self.purchase_reservation.pk, coach=self.main_coach,
            approved_for_reservation=self.lesson_reservation,
        )
        self.url = reverse("club:ticket_purchase_reverse", args=[self.purchase_reservation.pk]) + f"?availability_id={self.availability.pk}"

    def test_main_coach_uses_server_confirmation_and_customer_sees_reversed_history(self):
        self.client.force_login(self.main_coach)
        confirmation = self.client.get(self.url)
        self.assertEqual(confirmation.status_code, 200)
        self.assertContains(confirmation, "取消理由")
        self.assertContains(confirmation, "取消を実行")
        response = self.client.post(self.url, {"reason": "test"})
        self.assertEqual(response.status_code, 302)
        self.purchase_reservation.refresh_from_db()
        self.assertEqual(self.purchase_reservation.status, TicketPurchaseReservation.STATUS_REVERSED)
        self.client.force_login(self.member)
        self.assertContains(self.client.get(reverse("club:tickets")), "承認取消済み")

    def test_contractor_cannot_get_or_post_reversal(self):
        self.client.force_login(self.contractor)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {"reason": "test"}).status_code, 403)
        self.purchase_reservation.refresh_from_db()
        self.assertEqual(self.purchase_reservation.status, TicketPurchaseReservation.STATUS_APPROVED)
