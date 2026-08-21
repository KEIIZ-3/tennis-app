from datetime import datetime, timedelta
from types import SimpleNamespace

from django.test import SimpleTestCase
from django.utils import timezone

from club.lesson_execution_storage import is_held_finished_reservation
from club.settlement_calculator import aggregate_reservations


class _Consumptions:
    def filter(self, **_kwargs):
        return []


class ExecutedTicketRevenueTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.coach_1 = SimpleNamespace(pk=1, role="coach")
        self.coach_2 = SimpleNamespace(pk=2, role="coach")

    def _reservation(self, key, *, end_at=None, price=3500, coaches=None, guest_name=""):
        coaches = coaches or [self.coach_1]
        fixed_lesson = SimpleNamespace(pk=key, all_coaches=lambda: coaches)
        return SimpleNamespace(
            pk=key,
            fixed_lesson=fixed_lesson,
            availability=None,
            start_at=(end_at or self.now - timedelta(hours=1)) - timedelta(hours=1),
            end_at=end_at or self.now - timedelta(hours=1),
            participant_ticket_price_snapshot=price,
            ticket_consumptions=_Consumptions(),
            lesson_type="event",
            payment_amount=0,
            payment_status="not_required",
            substitute_coach=None,
            coach=self.coach_1,
            court_id=1,
            guest_name=guest_name,
            is_payment_tracking_required=lambda: False,
            assigned_coach=lambda: self.coach_1,
        )

    @staticmethod
    def _coach_row(coach):
        return {
            "coach": coach,
            "is_contractor_coach": False,
            "_lesson_slot_keys": set(),
            "reservation_count": 0,
            "contractor_work_slot_count": 0,
            "contractor_work_minutes": 0,
            "ticket_amount": 0,
            "preopen_paid_amount": 0,
            "preopen_waived_amount": 0,
            "preopen_unpaid_amount": 0,
        }

    def _aggregate(self, reservations, statuses):
        coach_map = {
            coach.pk: self._coach_row(coach)
            for coach in (self.coach_1, self.coach_2)
        }
        aggregate_reservations(
            reservations=reservations,
            coach_map=coach_map,
            reservation_model=SimpleNamespace(
                LESSON_GENERAL="general",
                PAYMENT_STATUS_PAID="paid",
                PAYMENT_STATUS_WAIVED="waived",
            ),
            preopen_cash_price=4000,
            is_preopen_cash_lesson_date=lambda _value: False,
            money=int,
            execution_status_map=statuses,
        )
        return coach_map

    def test_only_held_and_finished_reservations_are_ticket_revenue(self):
        held = self._reservation(1)
        active_unheld = self._reservation(2)
        future_held = self._reservation(3, end_at=self.now + timedelta(days=1))
        statuses = {
            "fixed:1:%s" % held.start_at.date(): {"status": "held"},
            "fixed:2:%s" % active_unheld.start_at.date(): {"status": "scheduled"},
            "fixed:3:%s" % future_held.start_at.date(): {"status": "held"},
        }

        rows = self._aggregate([held, active_unheld, future_held], statuses)

        self.assertEqual(rows[1]["ticket_amount"], 3500)
        self.assertEqual(rows[1]["reservation_count"], 1)
        self.assertTrue(is_held_finished_reservation(held, statuses))
        self.assertFalse(is_held_finished_reservation(active_unheld, statuses))
        self.assertFalse(is_held_finished_reservation(future_held, statuses))

    def test_guest_snapshot_price_and_multiple_coach_split_are_preserved(self):
        guest = self._reservation(
            4,
            price=5001,
            coaches=[self.coach_1, self.coach_2],
            guest_name="ゲスト",
        )
        statuses = {"fixed:4:%s" % guest.start_at.date(): {"status": "held"}}

        rows = self._aggregate([guest], statuses)

        self.assertEqual(rows[1]["ticket_amount"], 2500)
        self.assertEqual(rows[2]["ticket_amount"], 2500)
        self.assertEqual(rows[1]["reservation_count"], 1)
        self.assertEqual(rows[2]["reservation_count"], 1)

    def test_revenue_summary_and_settlement_share_the_same_execution_predicate(self):
        reservation = self._reservation(5, price=2345)
        held = {"fixed:5:%s" % reservation.start_at.date(): {"status": "held"}}
        canceled = {"fixed:5:%s" % reservation.start_at.date(): {"status": "canceled"}}
        rain_canceled = {"fixed:5:%s" % reservation.start_at.date(): {"status": "rain_canceled"}}

        self.assertTrue(is_held_finished_reservation(reservation, held))
        self.assertFalse(is_held_finished_reservation(reservation, canceled))
        self.assertFalse(is_held_finished_reservation(reservation, rain_canceled))
        self.assertEqual(self._aggregate([reservation], held)[1]["ticket_amount"], 2345)
        self.assertEqual(self._aggregate([reservation], canceled)[1]["ticket_amount"], 0)
        self.assertEqual(self._aggregate([reservation], rain_canceled)[1]["ticket_amount"], 0)
