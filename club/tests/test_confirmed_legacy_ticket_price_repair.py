import json
from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import call, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from club.confirmed_legacy_ticket_price_repair import (
    CONFIRMED_TARGETS,
    ConfirmedLegacyRepairRejected,
    apply_confirmed_legacy_prices,
    inspect_confirmed_legacy_prices,
)
from club.lesson_member_list import _member_row_from_reservation
from club.models import Court, Reservation, TicketConsumption, TicketLedger, TicketPurchase, User
from club.settlement_models import MonthlySettlement


class ConfirmedLegacyTicketPriceRepairTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(username="confirmed-coach", role=User.ROLE_COACH)
        self.court = Court.objects.create(name="confirmed court")
        self.reservations = {}
        self.purchases = []
        for reservation_id, target in CONFIRMED_TARGETS.items():
            user = User.objects.create_user(
                username=f"confirmed-{reservation_id}", full_name=target.member_name,
                ticket_balance=-1,
            )
            start = timezone.make_aware(
                datetime.combine(target.lesson_date, datetime.min.time()).replace(hour=10)
            )
            reservation = Reservation(
                id=reservation_id, user=user, coach=self.coach, court=self.court,
                start_at=start, end_at=start + timedelta(hours=1), tickets_used=1,
                status=Reservation.STATUS_ACTIVE, lesson_type=Reservation.LESSON_PRIVATE,
                ticket_consumed_at=start,
            )
            Reservation.objects.bulk_create([reservation])
            if target.price == 0:
                purchase = TicketPurchase.objects.create(
                    user=user, total_tickets=1, remaining_tickets=0,
                    unit_price=0, purchase_type=TicketPurchase.PURCHASE_TYPE_FORMAL_FREE,
                    purchased_at=start - timedelta(days=1),
                )
                self.purchases.append(purchase)
                TicketConsumption.objects.create(
                    user=user, purchase=purchase, reservation=reservation,
                    tickets_used=1, unit_price_snapshot=0,
                )
            elif reservation_id % 2:
                TicketConsumption.objects.create(
                    user=user, purchase=None, reservation=reservation,
                    tickets_used=1, unit_price_snapshot=None,
                )
            TicketLedger.objects.create(
                user=user, reservation=reservation, change_amount=-1,
                balance_after=-1, reason=TicketLedger.REASON_RESERVATION_USE,
            )
            self.reservations[reservation_id] = reservation
        outsider_user = User.objects.create_user(username="confirmed-outsider")
        outsider_start = timezone.make_aware(datetime(2026, 9, 15, 10))
        self.outsider = Reservation(
            id=2000, user=outsider_user, coach=self.coach, court=self.court,
            start_at=outsider_start, end_at=outsider_start + timedelta(hours=1),
            tickets_used=1, status=Reservation.STATUS_ACTIVE,
            lesson_type=Reservation.LESSON_PRIVATE,
        )
        Reservation.objects.bulk_create([self.outsider])

    def state(self):
        return {
            "balances": list(User.objects.order_by("id").values_list("id", "ticket_balance")),
            "ledgers": list(TicketLedger.objects.order_by("id").values_list("id", flat=True)),
            "purchases": list(TicketPurchase.objects.order_by("id").values_list(
                "id", "remaining_tickets", "total_tickets"
            )),
        }

    def test_dry_run_reports_all_targets_and_changes_nothing(self):
        before = self.state()
        stream = StringIO()
        call_command("repair_confirmed_legacy_ticket_prices", stdout=stream)
        payload = json.loads(stream.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["summary"], {
            "target_count": 12, "zero_price_count": 3, "price_3500_count": 9,
            "would_change_count": 12, "already_correct_count": 0, "blocked_count": 0,
        })
        for key in (
            "reservation_id", "member_name", "lesson_date", "current_snapshot",
            "new_snapshot", "current_consumptions", "planned_consumption_change",
            "would_change", "validation_result",
        ):
            self.assertIn(key, payload["rows"][0])
        self.assertEqual(before, self.state())
        self.assertFalse(Reservation.objects.exclude(participant_ticket_price_snapshot=None).exists())

    @patch("club.settlement_service.calculate_monthly_settlement")
    def test_apply_sets_confirmed_prices_without_changing_ticket_inventory(self, calculate):
        for month in (8, 9, 10):
            MonthlySettlement.objects.create(year=2026, month=month)
        before = self.state()

        apply_confirmed_legacy_prices()

        self.assertEqual(before, self.state())
        self.assertEqual(calculate.call_args_list, [
            call(2026, 8, force=True), call(2026, 9, force=True), call(2026, 10, force=True),
        ])
        for reservation_id, target in CONFIRMED_TARGETS.items():
            reservation = Reservation.objects.get(pk=reservation_id)
            consumption = TicketConsumption.objects.get(reservation=reservation)
            self.assertEqual(reservation.participant_ticket_price_snapshot, target.price)
            self.assertEqual(consumption.unit_price_snapshot, target.price)
            if target.price == 3500:
                self.assertIsNone(consumption.purchase_id)
        self.outsider.refresh_from_db()
        self.assertIsNone(self.outsider.participant_ticket_price_snapshot)
        self.assertFalse(TicketConsumption.objects.filter(reservation=self.outsider).exists())
        for reservation_id in (1660, 1661, 1662, 1653):
            self.assertEqual(
                Reservation.objects.get(pk=reservation_id).status,
                Reservation.STATUS_ACTIVE,
            )
        # The member-list amount is the persisted snapshot, including a real zero.
        for reservation_id in (1491, 1536):
            reservation = Reservation.objects.select_related("user").get(pk=reservation_id)
            member_row = _member_row_from_reservation(reservation)
            self.assertEqual(member_row["ticket_amount"], CONFIRMED_TARGETS[reservation_id].price)
            self.assertFalse(member_row["ticket_amount_is_unset"])

    def test_outside_id_and_existing_snapshot_are_not_changed(self):
        with self.assertRaises(ConfirmedLegacyRepairRejected):
            inspect_confirmed_legacy_prices(reservation_ids=[999999])
        reservation = self.reservations[1536]
        Reservation.objects.filter(pk=reservation.pk).update(participant_ticket_price_snapshot=4000)
        rows = inspect_confirmed_legacy_prices(reservation_ids=[reservation.pk])
        self.assertIn("snapshot_already_set", rows[0].validation_result)
        with self.assertRaises(ConfirmedLegacyRepairRejected):
            apply_confirmed_legacy_prices()
        reservation.refresh_from_db()
        self.assertEqual(reservation.participant_ticket_price_snapshot, 4000)

    def test_refunded_and_multiple_consumptions_block_the_whole_apply(self):
        row = TicketConsumption.objects.get(reservation_id=1535)
        row.refunded_at = timezone.now()
        row.save(update_fields=["refunded_at"])
        TicketConsumption.objects.create(
            user=self.reservations[1536].user, reservation_id=1536, tickets_used=1,
        )
        with self.assertRaises(ConfirmedLegacyRepairRejected):
            apply_confirmed_legacy_prices()
        self.assertFalse(Reservation.objects.exclude(participant_ticket_price_snapshot=None).exists())

    def test_closed_month_blocks_all_changes(self):
        MonthlySettlement.objects.create(
            year=2026, month=9, status=MonthlySettlement.STATUS_CLOSED,
        )
        with self.assertRaises(ConfirmedLegacyRepairRejected):
            apply_confirmed_legacy_prices()
        self.assertFalse(Reservation.objects.exclude(participant_ticket_price_snapshot=None).exists())

    def test_apply_is_atomic_if_a_write_fails(self):
        original_create = TicketConsumption.objects.create
        calls = 0

        def failing_create(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated write failure")
            return original_create(*args, **kwargs)

        with patch.object(TicketConsumption.objects, "create", failing_create):
            with self.assertRaises(RuntimeError):
                apply_confirmed_legacy_prices()
        self.assertFalse(Reservation.objects.exclude(participant_ticket_price_snapshot=None).exists())

    def test_apply_rejects_partial_selection_at_command_boundary(self):
        with self.assertRaises(CommandError):
            call_command(
                "repair_confirmed_legacy_ticket_prices", apply=True,
                reservation_id=[1491], stdout=StringIO(),
            )
