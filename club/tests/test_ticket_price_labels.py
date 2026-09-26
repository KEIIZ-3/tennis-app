from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from club.models import Court, Reservation, TicketConsumption, TicketPurchase, User


class TicketPriceLabelTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user(username="price-label-member")
        self.coach = User.objects.create_user(username="price-label-coach", role=User.ROLE_COACH)
        self.court = Court.objects.create(name="price-label-court")

    def test_ticket_purchase_unit_price_labels(self):
        self.assertEqual(TicketPurchase(unit_price=3500).unit_price_label(), "3500円券")
        self.assertEqual(TicketPurchase(unit_price=4000).unit_price_label(), "4000円券")
        self.assertEqual(TicketPurchase(unit_price=0).unit_price_label(), "0円券")

    def test_ticket_consumption_unit_price_labels(self):
        self.assertEqual(
            TicketConsumption(unit_price_snapshot=3500).unit_price_label(),
            "3500円券",
        )
        self.assertEqual(
            TicketConsumption(unit_price_snapshot=4000).unit_price_label(),
            "4000円券",
        )
        self.assertEqual(
            TicketConsumption(unit_price_snapshot=0).unit_price_label(),
            "0円券",
        )
        self.assertEqual(
            TicketConsumption(unit_price_snapshot=None).unit_price_label(),
            "価格不明券",
        )

    def test_reservation_breakdown_keeps_zero_and_unknown_in_separate_buckets(self):
        start_at = timezone.now() + timedelta(days=1)
        reservation = Reservation(
            user=self.member,
            coach=self.coach,
            court=self.court,
            start_at=start_at,
            end_at=start_at + timedelta(hours=1),
            tickets_used=4,
            lesson_type=Reservation.LESSON_PRIVATE,
        )
        Reservation.objects.bulk_create([reservation])
        TicketConsumption.objects.create(
            user=self.member,
            reservation=reservation,
            tickets_used=1,
            unit_price_snapshot=0,
        )
        TicketConsumption.objects.create(
            user=self.member,
            reservation=reservation,
            tickets_used=2,
            unit_price_snapshot=None,
        )
        TicketConsumption.objects.create(
            user=self.member,
            reservation=reservation,
            tickets_used=1,
            unit_price_snapshot=3500,
        )

        self.assertEqual(
            reservation.ticket_breakdown_items(),
            [
                {"unit_price": 0, "tickets": 1, "label": "0円券"},
                {"unit_price": 3500, "tickets": 1, "label": "3500円券"},
                {"unit_price": None, "tickets": 2, "label": "価格不明券"},
            ],
        )
