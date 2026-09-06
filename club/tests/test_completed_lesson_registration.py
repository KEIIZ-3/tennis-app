from datetime import datetime, timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from club.completed_lesson_registration import cancel_completed_lesson, register_completed_lesson
from club.lesson_calendar_service import build_lesson_calendar_display_data
from club.lesson_execution_storage import read_status_map
from club.models import (
    CoachExpense, CompletedLessonRegistration, Court, Reservation,
    TicketConsumption, TicketLedger, TicketPurchase, User,
)
from club.settlement_models import MonthlySettlement


class CompletedLessonRegistrationTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(username="completed-coach", role=User.ROLE_COACH)
        self.member1 = User.objects.create_user(username="completed-member-1", role=User.ROLE_MEMBER, ticket_balance=5)
        self.member2 = User.objects.create_user(username="completed-member-2", role=User.ROLE_MEMBER, ticket_balance=4)
        self.court = Court.objects.create(name="事後登録コート", available_court_count=2)
        self.start = timezone.make_aware(datetime(2026, 9, 6, 10, 0))
        self.end = self.start + timedelta(hours=1)
        self.lot1 = TicketPurchase.objects.create(user=self.member1, total_tickets=5, remaining_tickets=5, unit_price=2000, purchased_at=self.start-timedelta(days=1))
        self.lot2 = TicketPurchase.objects.create(user=self.member2, total_tickets=4, remaining_tickets=4, unit_price=2500, purchased_at=self.start-timedelta(days=1))

    def register_private(self, *, key="completed-private", participants=None, court_cost=3000):
        return register_completed_lesson(
            actor=self.coach, start_at=self.start, end_at=self.end,
            lesson_type=Reservation.LESSON_PRIVATE, coach=self.coach, court=self.court,
            participants=participants or [
                {"user": self.member1, "payment_method": "ticket", "value": 2},
                {"user": self.member2, "payment_method": "ticket", "value": 1},
            ], court_cost=court_cost, court_payer=self.coach, note="急な依頼", idempotency_key=key,
        )

    def test_private_two_members_uses_canonical_ticket_evidence_and_is_held(self):
        registration, created = self.register_private()
        self.assertTrue(created)
        reservations = list(Reservation.objects.filter(availability=registration.availability).order_by("id"))
        self.assertEqual([row.tickets_used for row in reservations], [2, 1])
        self.assertEqual(TicketConsumption.objects.filter(reservation__in=reservations, refunded_at__isnull=True).count(), 2)
        self.assertEqual(TicketLedger.objects.filter(reservation__in=reservations, change_amount__lt=0).count(), 2)
        self.member1.refresh_from_db(); self.member2.refresh_from_db()
        self.assertEqual((self.member1.ticket_balance, self.member2.ticket_balance), (3, 3))
        settlement = MonthlySettlement.objects.get(year=2026, month=9)
        self.assertEqual(read_status_map(settlement)[f"availability:{registration.availability_id}"]["status"], "held")
        self.assertTrue(registration.availability.is_recruitment_closed)

    def test_ticket_cash_guest_mix_and_idempotent_post(self):
        participants = [
            {"user": self.member1, "payment_method": "ticket", "value": 2},
            {"user": None, "guest_name": "ゲスト一郎", "payment_method": "cash", "value": 4000},
        ]
        registration, created = self.register_private(key="mixed", participants=participants)
        repeated, repeated_created = self.register_private(key="mixed", participants=participants)
        self.assertTrue(created); self.assertFalse(repeated_created)
        self.assertEqual(repeated.pk, registration.pk)
        self.assertEqual(Reservation.objects.filter(availability=registration.availability).count(), 2)
        guest = Reservation.objects.get(availability=registration.availability, user__isnull=True)
        self.assertEqual((guest.guest_name, guest.payment_method, guest.payment_amount), ("ゲスト一郎", "cash", 4000))

    def test_guest_ticket_duplicate_member_future_and_permissions_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.register_private(key="guest-ticket", participants=[{"user": None, "guest_name": "G", "payment_method": "ticket", "value": 1}])
        with self.assertRaises(ValidationError):
            self.register_private(key="duplicate", participants=[{"user": self.member1, "payment_method": "ticket", "value": 1}] * 2)
        member_actor = User.objects.create_user(username="member-actor", role=User.ROLE_MEMBER)
        with self.assertRaises(ValidationError):
            register_completed_lesson(actor=member_actor, start_at=self.start, end_at=self.end, lesson_type="private", coach=self.coach, court=self.court, participants=[{"user": self.member1,"payment_method":"ticket","value":1}], court_cost=0, court_payer=self.coach, note="", idempotency_key="forbidden")

    def test_cancel_refunds_tickets_reverses_expense_status_and_is_idempotent(self):
        registration, _ = self.register_private()
        expense_count = CoachExpense.objects.count()
        _row, changed = cancel_completed_lesson(registration_id=registration.pk, actor=self.coach)
        self.assertTrue(changed)
        self.member1.refresh_from_db(); self.member2.refresh_from_db(); self.lot1.refresh_from_db(); self.lot2.refresh_from_db()
        self.assertEqual((self.member1.ticket_balance, self.member2.ticket_balance), (5, 4))
        self.assertEqual((self.lot1.remaining_tickets, self.lot2.remaining_tickets), (5, 4))
        self.assertFalse(Reservation.objects.filter(availability=registration.availability, status=Reservation.STATUS_ACTIVE).exists())
        self.assertEqual(TicketConsumption.objects.filter(reservation__availability=registration.availability, refunded_at__isnull=True).count(), 0)
        self.assertEqual(CoachExpense.objects.count(), expense_count + 1)
        settlement = MonthlySettlement.objects.get(year=2026, month=9)
        self.assertNotIn(f"availability:{registration.availability_id}", read_status_map(settlement))
        _row, second_changed = cancel_completed_lesson(registration_id=registration.pk, actor=self.coach)
        self.assertFalse(second_changed)
        self.member1.refresh_from_db(); self.assertEqual(self.member1.ticket_balance, 5)

    def test_closed_month_blocks_registration_and_cancel(self):
        registration, _ = self.register_private()
        settlement = MonthlySettlement.objects.get(year=2026, month=9)
        settlement.status = MonthlySettlement.STATUS_CLOSED; settlement.save(update_fields=["status"])
        with self.assertRaises(ValidationError):
            cancel_completed_lesson(registration_id=registration.pk, actor=self.coach)

    def test_other_lesson_types_and_three_participants(self):
        for offset, lesson_type, hours in [(1, "general", 2), (2, "group", 1), (3, "event", 1)]:
            start = self.start + timedelta(hours=offset * 3)
            registration, _ = register_completed_lesson(
                actor=self.coach, start_at=start, end_at=start+timedelta(hours=hours),
                lesson_type=lesson_type, coach=self.coach, court=self.court,
                participants=[{"user": None, "guest_name": f"G{i}", "payment_method": "cash", "value": 1000} for i in range(3)],
                court_cost=0, court_payer=self.coach, note="", idempotency_key=f"type-{lesson_type}",
            )
            self.assertEqual(Reservation.objects.filter(availability=registration.availability, status="active").count(), 3)
