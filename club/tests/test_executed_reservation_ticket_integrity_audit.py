from datetime import datetime, timedelta
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from club.executed_reservation_ticket_integrity_audit import audit_executed_reservation_ticket_integrity
from club.lesson_execution_storage import save_status
from club.models import CoachAvailability, Court, Reservation, TicketConsumption, TicketPurchase, User
from club.settlement_models import MonthlySettlement


class ExecutedReservationTicketIntegrityAuditTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user(username="member", full_name="本人")
        self.coach = User.objects.create_user(username="coach", full_name="コーチ", role=User.ROLE_COACH)
        self.court = Court.objects.create(name="コート")
        self.settlement = MonthlySettlement.objects.create(year=2026, month=8)
        self.now = timezone.make_aware(datetime(2026, 8, 17, 23))

    def reservation(self, day, *, tickets=1, active=True, held=True, price=None):
        start = timezone.make_aware(datetime(2026, 8, day, 10))
        availability = CoachAvailability.objects.create(coach=self.coach, court=self.court, start_at=start, end_at=start + timedelta(hours=2), capacity=4)
        row = Reservation.objects.create(user=self.member, coach=self.coach, court=self.court, availability=availability, start_at=start, end_at=start + timedelta(hours=2), tickets_used=tickets, status=Reservation.STATUS_ACTIVE if active else Reservation.STATUS_CANCELED, participant_ticket_price_snapshot=price)
        if held:
            save_status(self.settlement, f"availability:{availability.pk}", "held", self.coach)
        return row

    def add_consumption(self, reservation, purchase_type, price=0, refunded=False):
        purchase = TicketPurchase.objects.create(user=self.member, purchase_type=purchase_type, total_tickets=1, remaining_tickets=0, unit_price=price)
        return TicketConsumption.objects.create(user=self.member, purchase=purchase, reservation=reservation, tickets_used=1, unit_price_snapshot=price, refunded_at=self.now if refunded else None)

    def test_reservation_is_source_of_truth_and_categories_are_exclusive(self):
        paid = self.reservation(2, price=3500)
        self.add_consumption(paid, TicketPurchase.PURCHASE_TYPE_SINGLE, 3500)
        free = self.reservation(3, price=0)
        self.add_consumption(free, TicketPurchase.PURCHASE_TYPE_FORMAL_FREE)
        adjustment = self.reservation(4, price=0)
        self.add_consumption(adjustment, TicketPurchase.PURCHASE_TYPE_ADMIN)
        legacy = self.reservation(5)
        self.add_consumption(legacy, TicketPurchase.PURCHASE_TYPE_LEGACY)
        missing = self.reservation(6, price=4000)
        refunded = self.reservation(7, price=3500)
        self.add_consumption(refunded, TicketPurchase.PURCHASE_TYPE_SINGLE, 3500, refunded=True)
        zero = self.reservation(8, tickets=0, price=0)
        Reservation.objects.filter(pk=zero.pk).update(tickets_used=0)
        self.reservation(9, active=False)
        self.reservation(10, held=False)

        with CaptureQueriesContext(connection) as queries:
            result = audit_executed_reservation_ticket_integrity(2026, 8, through_day=17, now=self.now)

        self.assertEqual(result["summary"]["executed_participant_count"], 7)
        self.assertEqual(result["summary"]["without_consumption_count"], 2)
        self.assertEqual(result["summary"]["recoverable_missing_revenue"], 4000)
        self.assertEqual(result["summary"]["current_executed_revenue"], 3500)
        self.assertEqual({row["integrity_classification"] for row in result["reservations"]}, {"paid", "formal_free", "adjustment_free", "legacy", "missing_consumption", "refunded", "zero_ticket"})
        self.assertTrue(next(row for row in result["reservations"] if row["reservation_id"] == missing.pk)["ball_expense_eligible"])
        self.assertTrue(all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries.captured_queries))

    def test_command_outputs_reservation_once(self):
        reservation = self.reservation(2, tickets=0, price=0)
        output = StringIO()
        call_command("audit_executed_reservation_ticket_integrity", 2026, 8, through_day=17, stdout=output)
        self.assertEqual(output.getvalue().count(f'"reservation_id": {reservation.pk}'), 1)
