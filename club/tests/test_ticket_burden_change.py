from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from club.models import (
    Court,
    Reservation,
    TicketBurdenChange,
    TicketConsumption,
    TicketLedger,
    TicketPurchase,
    User,
    purchase_tickets,
)
from club.ticket_burden_service import (
    _locked_reservations_queryset,
    change_lesson_ticket_burden,
)
from club.ticket_integrity_diagnostic import diagnose_ticket_integrity


class TicketBurdenChangeTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(username="main", role=User.ROLE_COACH)
        self.a = User.objects.create_user(username="a")
        self.b = User.objects.create_user(username="b")
        self.court = Court.objects.create(name="負担変更コート")
        self.start = timezone.now() + timedelta(days=20)
        for user in (self.a, self.b):
            purchase_tickets(
                user=user,
                tickets=4,
                unit_price=3500,
                purchase_type=TicketPurchase.PURCHASE_TYPE_SET4,
                reason=TicketLedger.REASON_PURCHASE_SET4,
            )
        self.ra = self._reservation(self.a)
        self.rb = self._reservation(self.b)
        self.ra.consume_tickets(created_by=self.coach)
        self.rb.consume_tickets(created_by=self.coach)

    def _reservation(self, user, *, lesson_type=Reservation.LESSON_GENERAL, tickets=1):
        row = Reservation(
            user=user,
            coach=self.coach,
            court=self.court,
            lesson_type=lesson_type,
            start_at=self.start,
            end_at=self.start + timedelta(hours=2),
            tickets_used=tickets,
            status=Reservation.STATUS_ACTIVE,
        )
        Reservation.objects.bulk_create([row])
        return row

    def test_move_restore_and_same_save_are_idempotent(self):
        change_lesson_ticket_burden(
            reservation_payers={self.ra.pk: self.a.pk, self.rb.pk: self.a.pk},
            created_by=self.coach,
        )
        self.a.refresh_from_db(); self.b.refresh_from_db()
        self.assertEqual((self.a.ticket_balance, self.b.ticket_balance), (2, 4))
        self.assertEqual(self.rb.tickets_used, 1)
        self.assertEqual(
            set(self.rb.ticket_consumptions.filter(refunded_at__isnull=True).values_list("user_id", flat=True)),
            {self.a.pk},
        )
        ledger_count = TicketLedger.objects.count()
        self.assertEqual(change_lesson_ticket_burden(
            reservation_payers={self.ra.pk: self.a.pk, self.rb.pk: self.a.pk},
            created_by=self.coach,
        ), [])
        self.assertEqual(TicketLedger.objects.count(), ledger_count)

        change_lesson_ticket_burden(
            reservation_payers={self.ra.pk: self.a.pk, self.rb.pk: self.b.pk},
            created_by=self.coach,
        )
        self.a.refresh_from_db(); self.b.refresh_from_db()
        self.assertEqual((self.a.ticket_balance, self.b.ticket_balance), (3, 3))
        self.assertEqual(TicketBurdenChange.objects.count(), 2)

    def test_cross_payer_cancel_refunds_actual_payer(self):
        change_lesson_ticket_burden(
            reservation_payers={self.ra.pk: self.a.pk, self.rb.pk: self.a.pk},
            created_by=self.coach,
        )
        self.rb.cancel(created_by=self.coach)
        self.a.refresh_from_db(); self.b.refresh_from_db()
        self.assertEqual((self.a.ticket_balance, self.b.ticket_balance), (3, 4))

    def test_guest_is_not_required_in_payer_mapping(self):
        self._reservation(None, tickets=1)
        changes = change_lesson_ticket_burden(
            reservation_payers={self.ra.pk: self.a.pk, self.rb.pk: self.a.pk},
            created_by=self.coach,
        )
        self.assertEqual(len(changes), 1)

    def test_rejects_payer_outside_lesson_participants(self):
        outsider = User.objects.create_user(username="outsider")
        with self.assertRaises(ValidationError):
            change_lesson_ticket_burden(
                reservation_payers={self.ra.pk: outsider.pk, self.rb.pk: self.b.pk},
                created_by=self.coach,
            )

    def test_locked_reservation_query_has_no_nullable_related_join(self):
        queryset = _locked_reservations_queryset([self.ra.pk, self.rb.pk])
        self.assertTrue(queryset.query.select_for_update)
        self.assertNotIn("JOIN", str(queryset.query).upper())

    def test_balance_floor_rolls_back_everything(self):
        self.a.ticket_balance = -4
        self.a.save(update_fields=["ticket_balance"])
        before = list(TicketConsumption.objects.values_list("id", "refunded_at"))
        with self.assertRaises(ValidationError):
            change_lesson_ticket_burden(
                reservation_payers={self.ra.pk: self.a.pk, self.rb.pk: self.a.pk},
                created_by=self.coach,
            )
        self.assertEqual(list(TicketConsumption.objects.values_list("id", "refunded_at")), before)
        self.assertEqual(TicketBurdenChange.objects.count(), 0)

    def test_zero_and_negative_balance_can_move_down_to_floor(self):
        for starting_balance in (0, -3):
            with self.subTest(starting_balance=starting_balance):
                self.a.ticket_balance = starting_balance
                self.a.save(update_fields=["ticket_balance"])
                change_lesson_ticket_burden(
                    reservation_payers={self.ra.pk: self.a.pk, self.rb.pk: self.a.pk},
                    created_by=self.coach,
                )
                self.a.refresh_from_db()
                self.assertEqual(self.a.ticket_balance, starting_balance - 1)
                change_lesson_ticket_burden(
                    reservation_payers={self.ra.pk: self.a.pk, self.rb.pk: self.b.pk},
                    created_by=self.coach,
                )

    def test_formal_cross_payer_is_valid_but_unrecorded_is_finding(self):
        change_lesson_ticket_burden(
            reservation_payers={self.ra.pk: self.a.pk, self.rb.pk: self.a.pk},
            created_by=self.coach,
        )
        reasons = {row["reason"] for row in diagnose_ticket_integrity()["consumption_findings"]}
        self.assertNotIn("reservation_user_mismatch", reasons)
        TicketBurdenChange.objects.all().delete()
        reasons = {row["reason"] for row in diagnose_ticket_integrity()["consumption_findings"]}
        self.assertIn("reservation_user_mismatch", reasons)

    def test_diagnostic_uses_latest_formal_payer(self):
        change_lesson_ticket_burden(
            reservation_payers={self.ra.pk: self.a.pk, self.rb.pk: self.a.pk},
            created_by=self.coach,
        )
        TicketBurdenChange.objects.create(
            reservation=self.rb,
            previous_payer=self.a,
            new_payer=self.b,
            tickets=1,
            created_by=self.coach,
        )
        reasons = {
            row["reason"]
            for row in diagnose_ticket_integrity()["consumption_findings"]
            if row.get("reservation_id") == self.rb.pk
        }
        self.assertIn("reservation_user_mismatch", reasons)


class GroupTicketCalculationTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(username="group-coach", role=User.ROLE_COACH)
        self.member = User.objects.create_user(username="group-member")
        self.court = Court.objects.create(name="group-court")
        self.start = timezone.now() + timedelta(days=30)

    def _row(self, status):
        return Reservation(
            user=self.member,
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GROUP,
            start_at=self.start,
            end_at=self.start + timedelta(hours=1),
            status=status,
        )

    def test_new_pending_activation_and_active_resave_count_once(self):
        new = self._row(Reservation.STATUS_ACTIVE)
        self.assertEqual(new.calculate_tickets_used(), 1)
        pending = self._row(Reservation.STATUS_PENDING)
        Reservation.objects.bulk_create([pending])
        pending.status = Reservation.STATUS_ACTIVE
        self.assertEqual(pending.calculate_tickets_used(), 1)
        Reservation.objects.filter(pk=pending.pk).update(status=Reservation.STATUS_ACTIVE)
        self.assertEqual(pending.calculate_tickets_used(), 1)
