import json
from datetime import datetime, timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from club.models import (
    Court,
    Reservation,
    ReservationParticipant,
    TicketConsumption,
    TicketPurchase,
    User,
)
from club.participant_price_integrity_diagnostic import diagnose_participant_price_integrity


class ParticipantPriceIntegrityDiagnosticTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user(
            username="private-member", email="secret@example.com", password="unused",
            full_name="Private Person", phone_number="090-0000-0000",
        )
        self.coach = User.objects.create_user(
            username="private-coach", password="unused", role=User.ROLE_COACH,
        )
        self.court = Court.objects.create(name="Diagnostic court")
        self.start = timezone.make_aware(datetime(2026, 8, 10, 10, 0))
        self.purchase_number = 0

    def _reservation(self, *, snapshot=None, tickets=1, status=Reservation.STATUS_ACTIVE,
                     custom=0, preopen=False, waived=False, user=None):
        start = timezone.make_aware(datetime(2026, 7, 10, 10, 0)) if preopen else self.start
        reservation = Reservation(
            user=user or self.member, coach=self.coach, court=self.court,
            lesson_type=Reservation.LESSON_GENERAL if preopen else Reservation.LESSON_PRIVATE,
            start_at=start, end_at=start + timedelta(hours=1), tickets_used=tickets,
            participant_ticket_price_snapshot=snapshot, status=status,
            custom_ticket_price=custom,
            payment_status=(Reservation.PAYMENT_STATUS_WAIVED if waived else Reservation.PAYMENT_STATUS_NOT_REQUIRED),
            payment_amount=2000 if preopen else 0,
        )
        Reservation.objects.bulk_create([reservation])
        return reservation

    def _consumption(self, reservation, price, tickets=1, *, refunded=False,
                     purchase_type=TicketPurchase.PURCHASE_TYPE_SINGLE,
                     legacy_auto=False):
        self.purchase_number += 1
        purchase = TicketPurchase.objects.create(
            user=reservation.user, purchase_type=purchase_type, total_tickets=tickets,
            remaining_tickets=0, unit_price=price,
            label="旧データ移行分" if legacy_auto else f"lot-{self.purchase_number}",
            note="既存残高との差分を補完" if legacy_auto else "",
        )
        return TicketConsumption.objects.create(
            user=reservation.user, purchase=purchase, reservation=reservation,
            tickets_used=tickets, unit_price_snapshot=price,
            refunded_at=timezone.now() if refunded else None,
        )

    def _db_snapshot(self):
        return {
            "reservations": list(Reservation.objects.order_by("id").values()),
            "purchases": list(TicketPurchase.objects.order_by("id").values()),
            "consumptions": list(TicketConsumption.objects.order_by("id").values()),
        }

    def test_null_legacy_a_b_c_and_fifo_classification(self):
        no_evidence = self._reservation(snapshot=None)
        single = self._reservation(snapshot=None)
        self._consumption(single, 4000)
        same = self._reservation(snapshot=None, tickets=2)
        self._consumption(same, 3500)
        self._consumption(same, 3500)
        mixed = self._reservation(snapshot=None, tickets=2)
        self._consumption(mixed, 3500)
        self._consumption(mixed, 4000)
        zero_legacy = self._reservation(snapshot=None)
        self._consumption(
            zero_legacy, 0, purchase_type=TicketPurchase.PURCHASE_TYPE_LEGACY,
            legacy_auto=True,
        )

        result = diagnose_participant_price_integrity()
        self.assertEqual(result["legacy_classification"], {
            "recoverable_a": 3, "conditional_b": 1, "unrecoverable_c": 1,
        })
        self.assertEqual(result["consumption_summary"]["none"], 1)
        self.assertEqual(result["consumption_summary"]["single"], 2)
        self.assertEqual(result["consumption_summary"]["multiple"], 2)
        self.assertEqual(result["consumption_summary"]["same_unit_price_multiple"], 1)
        self.assertEqual(result["consumption_summary"]["mixed_unit_price"], 1)
        self.assertEqual(result["multiple_lot_reservations"][1]["price_total"], 7500)
        self.assertEqual(result["special_cases"]["zero_price_legacy_auto_generated"], 1)
        self.assertEqual(result["finding_count"], 0)
        self.assertNotIn(no_evidence.pk, [row["reservation_id"] for row in result["multiple_lot_reservations"]])

    def test_snapshot_boundaries_match_mismatch_and_unverifiable(self):
        for price in (0, 1000, 1001):
            reservation = self._reservation(snapshot=price)
            self._consumption(reservation, price)
        mismatch = self._reservation(snapshot=4000)
        self._consumption(mismatch, 3500)
        unverifiable = self._reservation(snapshot=4000, tickets=2)
        self._consumption(unverifiable, 4000)

        result = diagnose_participant_price_integrity()
        self.assertEqual(result["snapshot_summary"]["zero"], 1)
        self.assertEqual(result["snapshot_summary"]["lte_1000"], 2)
        self.assertEqual(result["snapshot_summary"]["gt_1000"], 3)
        self.assertEqual(result["snapshot_verification"], {
            "match": 3, "mismatch": 1, "unverifiable": 1,
        })
        self.assertEqual(result["snapshot_mismatches"][0]["reservation_id"], mismatch.pk)
        self.assertEqual(result["integrity_findings"][0]["reservation_id"], unverifiable.pk)
        self.assertEqual(result["finding_count"], 2)

    def test_zero_ticket_custom_preopen_waived_and_returned_canceled(self):
        zero = self._reservation(snapshot=None, tickets=0)
        custom = self._reservation(snapshot=None, custom=2)
        self._consumption(custom, 4000)
        preopen = self._reservation(snapshot=None, tickets=0, preopen=True)
        waived = self._reservation(snapshot=None, waived=True)
        self._consumption(waived, 4000)
        canceled = self._reservation(snapshot=4000, status=Reservation.STATUS_CANCELED)
        self._consumption(canceled, 4000, refunded=True)

        result = diagnose_participant_price_integrity()
        special = result["special_cases"]
        self.assertEqual(special["zero_ticket_reservations"], 2)
        self.assertEqual(special["zero_ticket_snapshot_null"], 2)
        self.assertEqual(special["custom_ticket_price_snapshot_null"], 1)
        self.assertEqual(special["preopen_snapshot_null"], 1)
        self.assertEqual(special["waived_snapshot_null"], 1)
        self.assertEqual(result["consumption_summary"]["returned"], 1)
        self.assertEqual(result["snapshot_verification"]["match"], 1)
        self.assertEqual(result["finding_count"], 0)
        self.assertLess(zero.pk, preopen.pk)

    def test_family_reservations_are_counted_per_reservation_not_distinct_user(self):
        first = self._reservation(snapshot=None, tickets=0)
        second = self._reservation(snapshot=None, tickets=0)
        ReservationParticipant.objects.create(
            reservation=first, parent=self.member, participant_type="family",
            participant_name="Secret Child",
        )
        ReservationParticipant.objects.create(
            reservation=second, parent=self.member, participant_type="family",
            participant_name="Another Secret Child",
        )
        result = diagnose_participant_price_integrity()
        self.assertEqual(result["reservation_count"], 2)
        self.assertEqual(result["special_cases"]["family_reservations"], 2)
        self.assertEqual(result["special_cases"]["same_account_multiple_reservations"], 2)

    def test_command_is_deterministic_json_private_and_read_only_twice(self):
        matched = self._reservation(snapshot=4000)
        self._consumption(matched, 4000)
        mismatch = self._reservation(snapshot=3500)
        self._consumption(mismatch, 4000)
        before = self._db_snapshot()

        outputs = []
        for _ in range(2):
            stdout = StringIO()
            call_command("diagnose_participant_price_integrity", stdout=stdout)
            outputs.append(stdout.getvalue())

        self.assertEqual(outputs[0], outputs[1])
        parsed = json.loads(outputs[0])
        self.assertEqual(parsed["finding_count"], 1)
        self.assertEqual(self._db_snapshot(), before)
        for secret in (
            "private-member", "secret@example.com", "090-0000-0000",
            "Private Person", "Secret Child",
        ):
            self.assertNotIn(secret, outputs[0])
        self.assertEqual(
            [row["reservation_id"] for row in parsed["snapshot_mismatches"]],
            sorted(row["reservation_id"] for row in parsed["snapshot_mismatches"]),
        )

    def test_query_count_is_constant_per_dataset(self):
        for _ in range(5):
            reservation = self._reservation(snapshot=4000)
            self._consumption(reservation, 4000)
        with self.assertNumQueries(4):
            diagnose_participant_price_integrity()
