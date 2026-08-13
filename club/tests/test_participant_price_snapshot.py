from types import SimpleNamespace

from django.test import SimpleTestCase

from club.ball_expense_allocation import held_participant_count_by_coach
from club.participant_price_snapshot import (
    is_ball_expense_eligible,
    ticket_revenue_from_consumptions,
)


class ParticipantPriceSnapshotTests(SimpleTestCase):
    def test_consumption_revenue_is_participant_total_across_fifo_lots(self):
        consumptions = [
            SimpleNamespace(unit_price_snapshot=3500, tickets_used=1, refunded_at=None),
            SimpleNamespace(unit_price_snapshot=4000, tickets_used=2, refunded_at=None),
        ]

        self.assertEqual(ticket_revenue_from_consumptions(consumptions), 11500)

    def test_no_consumption_is_unknown_but_zero_price_consumption_is_zero(self):
        self.assertIsNone(ticket_revenue_from_consumptions([]))
        self.assertEqual(
            ticket_revenue_from_consumptions(
                [SimpleNamespace(unit_price_snapshot=0, tickets_used=1, refunded_at=None)]
            ),
            0,
        )

    def test_refunded_consumption_does_not_contribute_to_price(self):
        consumptions = [
            SimpleNamespace(unit_price_snapshot=4000, tickets_used=1, refunded_at=object()),
            SimpleNamespace(unit_price_snapshot=3500, tickets_used=1, refunded_at=None),
        ]

        self.assertEqual(ticket_revenue_from_consumptions(consumptions), 3500)

    def test_ball_expense_boundary_and_unknown_compatibility(self):
        expected = {
            None: True,
            0: False,
            800: False,
            1000: False,
            1001: True,
            1200: True,
            2000: True,
            4000: True,
        }
        for price, is_eligible in expected.items():
            with self.subTest(price=price):
                reservation = SimpleNamespace(
                    participant_ticket_price_snapshot=price
                )
                self.assertEqual(is_ball_expense_eligible(reservation), is_eligible)

    def test_held_count_filters_each_reservation_price_individually(self):
        coach = SimpleNamespace(pk=1)
        reservations = [
            SimpleNamespace(pk=1, slot="held", participant_ticket_price_snapshot=4000),
            SimpleNamespace(pk=2, slot="held", participant_ticket_price_snapshot=1000),
            SimpleNamespace(pk=3, slot="held", participant_ticket_price_snapshot=800),
            SimpleNamespace(pk=4, slot="scheduled", participant_ticket_price_snapshot=4000),
        ]

        counts = held_participant_count_by_coach(
            reservations,
            {
                "held": {"status": "held"},
                "scheduled": {"status": "scheduled"},
            },
            eligible_coach_ids=[1],
            execution_slot_key=lambda reservation: reservation.slot,
            reservation_coaches=lambda reservation: [coach],
        )

        self.assertEqual(counts, {1: 1})

    def test_mixed_prices_above_boundary_are_all_counted(self):
        coach = SimpleNamespace(pk=1)
        reservations = [
            SimpleNamespace(pk=index, slot="held", participant_ticket_price_snapshot=price)
            for index, price in enumerate((4000, 2000, 1001), start=1)
        ]

        counts = held_participant_count_by_coach(
            reservations,
            {"held": {"status": "held"}},
            eligible_coach_ids=[1],
            execution_slot_key=lambda reservation: reservation.slot,
            reservation_coaches=lambda reservation: [coach],
        )

        self.assertEqual(counts, {1: 3})
