import json
from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db.models.query import QuerySet
from django.test import TestCase
from django.utils import timezone

from club.models import Court, Reservation, TicketConsumption, TicketLedger, TicketPurchase, User
from club.participant_price_snapshot import is_ball_expense_eligible
from club.participant_price_snapshot_repair import (
    inspect_participant_price_snapshot_repair, repair_participant_price_snapshot,
)


class ParticipantPriceSnapshotRepairTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user(username="akagi", full_name="赤木 琴江")
        self.coach = User.objects.create_user(username="coach", role=User.ROLE_COACH)
        self.court = Court.objects.create(name="Snapshot repair court")
        self.start = timezone.make_aware(datetime(2026, 8, 2, 10))
        self.reservation_number = 0

    def reservation(self, *, tickets=1, snapshot=None, days=0, user=None, reservation_id=None):
        self.reservation_number += 1
        start = self.start + timedelta(days=days, hours=self.reservation_number)
        reservation = Reservation(
            id=reservation_id,
            user=user or self.member, coach=self.coach, court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE,
            start_at=start, end_at=start + timedelta(hours=1),
            tickets_used=tickets, participant_ticket_price_snapshot=snapshot,
        )
        Reservation.objects.bulk_create([reservation])
        return reservation

    def evidence(self, reservation, price, *, tickets=1, refunded=False, purchase=None, note=""):
        purchase = purchase or TicketPurchase.objects.create(
            user=reservation.user, purchase_type=TicketPurchase.PURCHASE_TYPE_SET4,
            total_tickets=4, remaining_tickets=0, unit_price=price, label="4枚セット", note=note,
        )
        return TicketConsumption.objects.create(
            user=reservation.user, reservation=reservation, purchase=purchase,
            tickets_used=tickets, unit_price_snapshot=price,
            refunded_at=timezone.now() if refunded else None,
        )

    def protected_state(self):
        return {
            "balance": User.objects.get(pk=self.member.pk).ticket_balance,
            "ledger": list(TicketLedger.objects.order_by("id").values()),
            "purchases": list(TicketPurchase.objects.order_by("id").values()),
            "consumptions": list(TicketConsumption.objects.order_by("id").values()),
            "court": list(Court.objects.order_by("id").values()),
        }

    def test_1510_and_1528_equivalents_repair_from_purchase_18(self):
        purchase18 = TicketPurchase.objects.create(
            user=self.member, purchase_type=TicketPurchase.PURCHASE_TYPE_SET4,
            total_tickets=4, remaining_tickets=0, unit_price=3500, label="4枚セット",
            note="管理画面から4枚セット付与",
        )
        for reservation_id, days in ((1510, 0), (1528, 14)):
            reservation = self.reservation(days=days, reservation_id=reservation_id)
            self.evidence(reservation, 3500, purchase=purchase18)
            before = self.protected_state()
            result = repair_participant_price_snapshot(reservation.pk)
            reservation.refresh_from_db()
            self.assertTrue(result.candidate)
            self.assertEqual(reservation.participant_ticket_price_snapshot, 3500)
            self.assertTrue(is_ball_expense_eligible(reservation))
            self.assertEqual(self.protected_state(), before)

    def test_1527_zero_free_ticket_is_excluded_and_not_repriced(self):
        reservation = self.reservation()
        self.evidence(reservation, 0, note="コート取得ご協力")
        result = repair_participant_price_snapshot(reservation.pk)
        reservation.refresh_from_db()
        self.assertEqual(result.reason, "zero_price_excluded")
        self.assertIsNone(reservation.participant_ticket_price_snapshot)
        reservation.participant_ticket_price_snapshot = 0
        self.assertFalse(is_ball_expense_eligible(reservation))

    def test_boundaries_and_future_family_evidence(self):
        for price, eligible in ((3500, True), (1000, False), (500, False), (0, False)):
            reservation = self.reservation(snapshot=price)
            self.assertEqual(is_ball_expense_eligible(reservation), eligible)
        family_user = User.objects.create_user(username="family")
        future = self.reservation(days=60, user=family_user)
        self.evidence(future, 3500)
        result = repair_participant_price_snapshot(future.pk)
        future.refresh_from_db()
        self.assertTrue(result.candidate)
        self.assertEqual(future.participant_ticket_price_snapshot, 3500)

    def test_rejection_rules_and_existing_snapshot_noop(self):
        mixed = self.reservation(tickets=2)
        self.evidence(mixed, 3500)
        self.evidence(mixed, 4000)
        self.assertEqual(inspect_participant_price_snapshot_repair(mixed.pk).reason, "mixed_prices")

        incomplete = self.reservation(tickets=2)
        self.evidence(incomplete, 3500)
        self.assertEqual(inspect_participant_price_snapshot_repair(incomplete.pk).reason, "consumption_ticket_count_mismatch")

        refunded = self.reservation()
        self.evidence(refunded, 3500, refunded=True)
        self.assertEqual(inspect_participant_price_snapshot_repair(refunded.pk).reason, "refunded_consumption")

        existing = self.reservation(snapshot=3500)
        self.evidence(existing, 3500)
        self.assertEqual(inspect_participant_price_snapshot_repair(existing.pk).reason, "snapshot_already_set")

    def test_command_defaults_to_dry_run_and_apply_changes_only_snapshot(self):
        reservation = self.reservation()
        self.evidence(reservation, 3500)
        before = self.protected_state()
        output = StringIO()
        call_command("repair_participant_price_snapshots", reservation_id=[reservation.pk], stdout=output)
        self.assertTrue(json.loads(output.getvalue())["dry_run"])
        reservation.refresh_from_db()
        self.assertIsNone(reservation.participant_ticket_price_snapshot)
        call_command("repair_participant_price_snapshots", reservation_id=[reservation.pk], apply=True, stdout=StringIO())
        reservation.refresh_from_db()
        self.assertEqual(reservation.participant_ticket_price_snapshot, 3500)
        self.assertEqual(self.protected_state(), before)

    def test_apply_locks_consumptions_without_locking_nullable_purchase_join(self):
        reservation = self.reservation()
        self.evidence(reservation, 3500)
        observed_queries = []

        original_fetch_all = QuerySet._fetch_all

        def capture_fetch_all(queryset):
            if queryset.model is TicketConsumption and queryset.query.select_for_update:
                observed_queries.append(queryset.query)
            return original_fetch_all(queryset)

        with patch("django.db.models.query.QuerySet._fetch_all", capture_fetch_all):
            result = repair_participant_price_snapshot(reservation.pk)

        self.assertTrue(result.candidate)
        self.assertTrue(observed_queries)
        for query in observed_queries:
            self.assertEqual(query.select_for_update_of, ("self",))
            self.assertIn("purchase", query.select_related)

    def test_consumption_missing_is_rejected(self):
        reservation = self.reservation()

        result = repair_participant_price_snapshot(reservation.pk)

        self.assertEqual(result.reason, "consumption_missing")
        reservation.refresh_from_db()
        self.assertIsNone(reservation.participant_ticket_price_snapshot)
