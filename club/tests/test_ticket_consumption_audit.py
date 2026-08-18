from datetime import datetime, timedelta

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from club.forms import TicketGrantAdminForm
from club.lesson_execution_storage import save_status
from club.models import (
    CoachAvailability, Court, FamilyMember, Reservation, ReservationParticipant,
    TicketConsumption, TicketPurchase, User,
)
from club.settlement_models import MonthlySettlement
from club.ticket_consumption_audit import audit_ticket_consumptions


class TicketConsumptionAuditTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user(username="audit-member", full_name="会員本人")
        self.coach = User.objects.create_user(username="audit-coach", full_name="担当コーチ", role=User.ROLE_COACH)
        self.court = Court.objects.create(name="監査コート")
        self.settlement = MonthlySettlement.objects.create(year=2026, month=8)
        self.now = timezone.make_aware(datetime(2026, 8, 17, 12))

    def consumption(self, day, price, purchase_type, *, refunded=False, status=Reservation.STATUS_ACTIVE):
        start = timezone.make_aware(datetime(2026, 8, day, 10))
        availability = CoachAvailability.objects.create(
            coach=self.coach, court=self.court, start_at=start,
            end_at=start + timedelta(hours=2), capacity=4,
        )
        reservation = Reservation.objects.create(
            user=self.member, coach=self.coach, court=self.court,
            availability=availability, start_at=start, end_at=start + timedelta(hours=2),
            status=status, tickets_used=1,
        )
        purchase = TicketPurchase.objects.create(
            user=self.member, purchase_type=purchase_type, total_tickets=1,
            remaining_tickets=0, unit_price=price,
        )
        consumption = TicketConsumption.objects.create(
            user=self.member, purchase=purchase, reservation=reservation,
            tickets_used=1, unit_price_snapshot=price,
            refunded_at=self.now if refunded else None,
        )
        return reservation, consumption

    def test_detail_classification_lifecycle_values_and_family_participant(self):
        held, paid = self.consumption(2, 3500, TicketPurchase.PURCHASE_TYPE_SET4)
        future, free = self.consumption(23, 0, TicketPurchase.PURCHASE_TYPE_FORMAL_FREE)
        _legacy_reservation, legacy = self.consumption(3, 0, TicketPurchase.PURCHASE_TYPE_LEGACY, refunded=True, status=Reservation.STATUS_CANCELED)
        family = FamilyMember.objects.create(
            parent=self.member, full_name="家族参加者",
            member_level=User.LEVEL_BEGINNER,
        )
        ReservationParticipant.objects.create(
            reservation=future, parent=self.member, family_member=family,
            participant_type="family", participant_name="家族参加者",
        )
        save_status(self.settlement, f"availability:{held.availability_id}", "held", self.coach)

        with CaptureQueriesContext(connection) as queries:
            result = audit_ticket_consumptions(2026, 8, now=self.now)

        rows = {row["consumption_id"]: row for row in result["consumptions"]}
        self.assertEqual(rows[paid.pk]["classification"], "paid")
        self.assertEqual(rows[paid.pk]["lifecycle_state"], "executed")
        self.assertEqual(rows[free.pk]["classification"], "formal_free")
        self.assertEqual(rows[free.pk]["participant_name"], "家族参加者")
        self.assertEqual(rows[free.pk]["consumption_value"], 0)
        self.assertTrue(rows[legacy.pk]["returned"])
        self.assertEqual(result["summary"]["consumed_ticket_count"], 3)
        self.assertEqual(result["summary"]["executed_paid_consumption_value"], 3500)
        self.assertEqual(result["summary"]["future_paid_consumption_value"], 0)
        self.assertEqual(result["summary"]["returned_ticket_count"], 1)
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries.captured_queries))

    def test_command_outputs_each_consumption_once(self):
        _reservation, consumption = self.consumption(23, 4000, TicketPurchase.PURCHASE_TYPE_SINGLE)
        from io import StringIO
        output = StringIO()
        call_command("audit_ticket_consumptions", 2026, 8, stdout=output)
        self.assertEqual(output.getvalue().count(f'"consumption_id": {consumption.pk}'), 1)

    def test_missing_purchase_evidence_is_unverifiable_without_value_inference(self):
        start = timezone.make_aware(datetime(2026, 8, 4, 10))
        availability = CoachAvailability.objects.create(
            coach=self.coach, court=self.court, start_at=start,
            end_at=start + timedelta(hours=2), capacity=4,
        )
        reservation = Reservation.objects.create(
            user=self.member, coach=self.coach, court=self.court,
            availability=availability, start_at=start,
            end_at=start + timedelta(hours=2), tickets_used=1,
        )
        consumption = TicketConsumption.objects.create(
            user=self.member, purchase=None, reservation=reservation,
            tickets_used=1, unit_price_snapshot=None,
        )

        result = audit_ticket_consumptions(2026, 8, now=self.now)
        row = result["consumptions"][0]

        self.assertEqual(row["consumption_id"], consumption.pk)
        self.assertEqual(row["classification"], "unverifiable")
        self.assertEqual(row["purchase_evidence"], "missing_purchase_evidence")
        self.assertIsNone(row["purchase_id"])
        self.assertIsNone(row["purchase_type"])
        self.assertIsNone(row["purchase_unit_price"])
        self.assertEqual(row["consumption_value"], 0)
        self.assertEqual(result["summary"]["unverifiable_ticket_count"], 1)
        self.assertEqual(result["summary"]["executed_paid_consumption_value"], 0)
        self.assertEqual(result["summary"]["active_inventory_value"], 0)

    def test_refunded_missing_purchase_keeps_refunded_lifecycle(self):
        start = timezone.make_aware(datetime(2026, 8, 5, 10))
        availability = CoachAvailability.objects.create(
            coach=self.coach, court=self.court, start_at=start,
            end_at=start + timedelta(hours=2), capacity=4,
        )
        reservation = Reservation.objects.create(
            user=self.member, coach=self.coach, court=self.court,
            availability=availability, start_at=start, end_at=start + timedelta(hours=2),
            status=Reservation.STATUS_CANCELED, tickets_used=1,
        )
        TicketConsumption.objects.create(
            user=self.member, purchase=None, reservation=reservation,
            tickets_used=1, unit_price_snapshot=None, refunded_at=self.now,
        )

        result = audit_ticket_consumptions(2026, 8, now=self.now)

        self.assertEqual(result["consumptions"][0]["classification"], "unverifiable")
        self.assertEqual(result["consumptions"][0]["lifecycle_state"], "refunded")
        self.assertEqual(result["summary"]["returned_ticket_count"], 1)

    def test_admin_adjustment_classification_is_preserved(self):
        _reservation, consumption = self.consumption(
            6, 0, TicketPurchase.PURCHASE_TYPE_ADMIN,
        )

        result = audit_ticket_consumptions(2026, 8, now=self.now)

        self.assertEqual(result["consumptions"][0]["consumption_id"], consumption.pk)
        self.assertEqual(result["consumptions"][0]["classification"], "adjustment")
        self.assertEqual(result["summary"]["adjustment_ticket_count"], 1)


class TicketGrantClassificationTests(TestCase):
    def form(self, kind, price):
        return TicketGrantAdminForm(data={
            "idempotency_token": "00000000-0000-0000-0000-000000000001",
            "grant_kind": kind, "tickets": 1, "unit_price": price,
            "label": "", "note": "",
        })

    def test_formal_free_is_structural_and_requires_zero_price(self):
        form = self.form("formal_free", 0)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.resolved_purchase_type(), TicketPurchase.PURCHASE_TYPE_FORMAL_FREE)
        self.assertFalse(self.form("formal_free", 1).is_valid())

    def test_paid_zero_price_is_rejected_and_adjustment_remains_distinct(self):
        self.assertFalse(self.form("paid", 0).is_valid())
        form = self.form("adjustment", 0)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.resolved_purchase_type(), TicketPurchase.PURCHASE_TYPE_ADMIN)
