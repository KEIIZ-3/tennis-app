import json
from datetime import date, datetime, timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from club.models import Court, Reservation, TicketConsumption, TicketPurchase, User
from club.participant_price_backfill_impact import diagnose_participant_price_backfill_impact
from club.participant_price_integrity_diagnostic import diagnose_participant_price_integrity
from club.settlement_models import MonthlySettlement


class ParticipantPriceBackfillImpactTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user(
            username="private-member", email="secret@example.com", password="unused",
            full_name="Private Person", phone_number="090-0000-0000",
        )
        self.coach = User.objects.create_user(
            username="main-coach", password="unused", role=User.ROLE_COACH,
        )
        self.coach_2 = User.objects.create_user(
            username="second-coach", password="unused", role=User.ROLE_COACH,
        )
        self.contractor = User.objects.create_user(
            username="contractor", password="unused", role=User.ROLE_CONTRACTOR_COACH,
        )
        self.court = Court.objects.create(name="Impact court")
        self.start = timezone.make_aware(datetime(2026, 7, 10, 10, 0))
        self.purchase_number = 0

    def _reservation(self, price, *, status=Reservation.STATUS_ACTIVE, coach=None):
        reservation = Reservation(
            user=self.member, coach=self.coach, substitute_coach=coach,
            court=self.court, lesson_type=Reservation.LESSON_PRIVATE,
            start_at=self.start, end_at=self.start + timedelta(hours=1), tickets_used=1,
            participant_ticket_price_snapshot=None, status=status,
            payment_status=Reservation.PAYMENT_STATUS_NOT_REQUIRED,
        )
        Reservation.objects.bulk_create([reservation])
        self.purchase_number += 1
        purchase = TicketPurchase.objects.create(
            user=self.member, purchase_type=TicketPurchase.PURCHASE_TYPE_SINGLE,
            total_tickets=1, remaining_tickets=0, unit_price=price,
            label=f"lot-{self.purchase_number}",
        )
        TicketConsumption.objects.create(
            user=self.member, purchase=purchase, reservation=reservation,
            tickets_used=1, unit_price_snapshot=price,
        )
        return reservation

    def _db_state(self):
        return {
            "reservations": list(Reservation.objects.order_by("id").values()),
            "consumptions": list(TicketConsumption.objects.order_by("id").values()),
            "settlements": list(MonthlySettlement.objects.order_by("id").values()),
        }

    def test_boundaries_execution_coaches_counts_and_allocations(self):
        unchanged_4000 = self._reservation(4000)
        excluded_1000 = self._reservation(1000)
        excluded_800 = self._reservation(800, coach=self.contractor)
        unchanged_1001 = self._reservation(1001)
        canceled = self._reservation(800, status=Reservation.STATUS_CANCELED)
        zero = self._reservation(0)
        baseline = self._reservation(4000, coach=self.coach_2)
        Reservation.objects.filter(pk=baseline.pk).update(
            participant_ticket_price_snapshot=4000
        )
        baseline.participant_ticket_price_snapshot = 4000
        settlement = MonthlySettlement.objects.create(year=2026, month=7)
        active = [unchanged_4000, excluded_1000, excluded_800, unchanged_1001, baseline]
        status_map = {
            f"slot:{unchanged_4000.pk}": {"status": "held"},
            f"slot:{excluded_1000.pk}": {"status": "held"},
            f"slot:{excluded_800.pk}": {"status": "held"},
            f"slot:{unchanged_1001.pk}": {"status": "scheduled"},
            f"slot:{baseline.pk}": {"status": "held"},
        }
        expense = SimpleNamespace(pk=71, category="ball")
        approved = [{
            "expense": expense, "amount": 9000, "payer_id": self.coach.pk,
            "is_court": False,
        }]

        with (
            patch("club.participant_price_backfill_impact.main_coaches", return_value=[self.coach, self.coach_2]),
            patch("club.participant_price_backfill_impact._execution_slot_key", side_effect=lambda r: f"slot:{r.pk}"),
            patch("club.participant_price_backfill_impact._monthly_execution_reservations_and_status", return_value=(active, status_map)),
            patch("club.participant_price_backfill_impact._approved_monthly_expenses", return_value=approved),
        ):
            result = diagnose_participant_price_backfill_impact()

        self.assertEqual(result["recoverable_count"], 5)
        self.assertEqual(diagnose_participant_price_integrity()["legacy_classification"]["recoverable_a"], 5)
        self.assertEqual(result["price_distribution"], {"lte_1000": 3, "gt_1000": 2})
        rows = {row["reservation_id"]: row for row in result["reservations"]}
        self.assertFalse(rows[unchanged_4000.pk]["eligibility_changes"])
        self.assertTrue(rows[excluded_1000.pk]["eligibility_changes"])
        self.assertTrue(rows[excluded_800.pk]["eligibility_changes"])
        self.assertFalse(rows[unchanged_1001.pk]["eligibility_changes"])
        self.assertFalse(rows[canceled.pk]["is_ball_expense_participant"])
        self.assertEqual(rows[unchanged_1001.pk]["lesson_execution_status"], "scheduled")
        self.assertEqual(rows[excluded_800.pk]["effective_coach_ids"], [self.contractor.pk])
        month = result["monthly_impact"][0]
        self.assertEqual(month["settlement_status"], "open")
        self.assertTrue(month["count_changed"])
        self.assertTrue(month["ball_expense_allocation_changed"])
        allocation = month["ball_expenses"][0]
        self.assertEqual(allocation["before_total"], 9000)
        self.assertEqual(allocation["after_total"], 9000)
        self.assertNotEqual(allocation["before_allocation"], allocation["after_allocation"])
        self.assertNotIn(zero.pk, rows)

    def test_all_above_threshold_without_expense_has_no_effect_and_closed_status(self):
        reservation = self._reservation(4000)
        MonthlySettlement.objects.create(
            year=2026, month=7, status=MonthlySettlement.STATUS_CLOSED
        )
        with (
            patch("club.participant_price_backfill_impact.main_coaches", return_value=[self.coach]),
            patch("club.participant_price_backfill_impact._execution_slot_key", return_value="slot:held"),
            patch("club.participant_price_backfill_impact._monthly_execution_reservations_and_status", return_value=([reservation], {"slot:held": {"status": "held"}})),
            patch("club.participant_price_backfill_impact._approved_monthly_expenses", return_value=[]),
        ):
            result = diagnose_participant_price_backfill_impact()
        self.assertEqual(result["backfill_effect_summary"], {
            "eligibility_changes": 0,
            "months_with_participant_count_changes": 0,
            "months_with_ball_allocation_changes": 0,
        })
        self.assertEqual(result["monthly_impact"][0]["settlement_status"], "closed")

    def test_command_is_deterministic_private_json_and_read_only(self):
        reservation = self._reservation(4000)
        before = self._db_state()
        outputs = []
        with (
            patch("club.participant_price_backfill_impact.main_coaches", return_value=[self.coach]),
            patch("club.participant_price_backfill_impact._execution_slot_key", return_value="slot:held"),
            patch("club.participant_price_backfill_impact._monthly_execution_reservations_and_status", return_value=([reservation], {"slot:held": {"status": "held"}})),
            patch("club.participant_price_backfill_impact._approved_monthly_expenses", return_value=[]),
        ):
            for _ in range(2):
                stdout = StringIO()
                call_command("diagnose_participant_price_backfill_impact", stdout=stdout)
                outputs.append(stdout.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        json.loads(outputs[0])
        self.assertEqual(self._db_state(), before)
        for secret in ("private-member", "secret@example.com", "090-0000-0000", "Private Person"):
            self.assertNotIn(secret, outputs[0])
