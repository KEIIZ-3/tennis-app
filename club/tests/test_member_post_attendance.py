from datetime import timedelta
from unittest.mock import call, patch

from django.core.exceptions import ValidationError
from django.db import models
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.lesson_execution_storage import save_status
from club.models import (
    CoachAvailability,
    Court,
    FixedLesson,
    Reservation,
    TicketBurdenChange,
    TicketConsumption,
    TicketLedger,
    TicketPurchase,
    User,
)
from club.participant_accounting import add_member_to_lesson_occurrence
from club.reservation_service import cancel_reservation_with_settlement
from club.settlement_models import CoachMonthlySettlement, MonthlySettlement
from club.settlement_service import calculate_monthly_settlement, get_or_create_monthly_settlement
from club.ticket_burden_service import change_lesson_ticket_burden


class MemberPostAttendanceTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(
            username="post-attendance-coach", role=User.ROLE_COACH
        )
        self.member_a = User.objects.create_user(
            username="post-attendance-a",
            role=User.ROLE_MEMBER,
            member_level=User.LEVEL_ADVANCED,
            ticket_balance=2,
        )
        self.member_b = User.objects.create_user(
            username="post-attendance-b",
            role=User.ROLE_MEMBER,
            ticket_balance=1,
        )
        self.court = Court.objects.create(name="事後参加コート", is_active=True)
        self.start = (timezone.localtime(timezone.now()) - timedelta(days=2)).replace(
            hour=18, minute=0, second=0, microsecond=0
        )
        self.availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_ADVANCED,
            start_at=self.start,
            end_at=self.start + timedelta(hours=2),
            capacity=5,
        )
        self.existing = Reservation.objects.create(
            user=self.member_a,
            coach=self.coach,
            court=self.court,
            availability=self.availability,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_ADVANCED,
            start_at=self.availability.start_at,
            end_at=self.availability.end_at,
        )
        TicketPurchase.objects.create(
            user=self.member_b,
            total_tickets=1,
            remaining_tickets=1,
            unit_price=3500,
            purchased_at=self.start - timedelta(days=1),
        )
        settlement = get_or_create_monthly_settlement(self.start.year, self.start.month)
        save_status(
            settlement,
            f"availability:{self.availability.pk}",
            "held",
            self.coach,
        )

    def add_member(self, member=None, availability=None):
        return add_member_to_lesson_occurrence(
            actor=self.coach,
            member=member or self.member_b,
            availability=availability or self.availability,
        )

    def test_adds_to_existing_occurrence_and_consumes_ticket_without_notification(self):
        availability_count = CoachAvailability.objects.count()
        with patch(
            "club.signals.schedule_reservation_canceled_notification"
        ) as notify:
            reservation = self.add_member()

        reservation.refresh_from_db()
        self.member_b.refresh_from_db()
        self.assertEqual(CoachAvailability.objects.count(), availability_count)
        self.assertEqual(reservation.availability_id, self.availability.pk)
        self.assertEqual(reservation.tickets_used, 1)
        self.assertIsNotNone(reservation.ticket_consumed_at)
        self.assertEqual(reservation.participant_ticket_price_snapshot, 3500)
        self.assertEqual(self.member_b.ticket_balance, 0)
        self.assertEqual(
            TicketConsumption.objects.get(reservation=reservation).tickets_used, 1
        )
        self.assertEqual(
            TicketLedger.objects.get(reservation=reservation).change_amount, -1
        )
        self.assertEqual(
            Reservation.objects.filter(
                availability=self.availability, status=Reservation.STATUS_ACTIVE
            ).count(),
            2,
        )
        notify.assert_not_called()

    def test_locks_only_availability_with_nullable_related_rows(self):
        self.assertIsNone(self.availability.substitute_coach_id)
        self.assertIsNone(self.availability.fixed_lesson_source_id)
        select_for_update = CoachAvailability.objects.select_for_update

        with patch.object(
            CoachAvailability.objects,
            "select_for_update",
            wraps=select_for_update,
        ) as lock:
            reservation = self.add_member()

        self.assertEqual(lock.call_args_list[0], call(of=("self",)))
        self.assertEqual(reservation.availability_id, self.availability.pk)

    def test_fifo_consumption_records_complete_evidence_and_purchase_price(self):
        TicketPurchase.objects.filter(user=self.member_b).delete()
        old = TicketPurchase.objects.create(
            user=self.member_b, total_tickets=1, remaining_tickets=1,
            unit_price=3500, purchased_at=self.start - timedelta(days=10),
        )
        new = TicketPurchase.objects.create(
            user=self.member_b, total_tickets=4, remaining_tickets=4,
            unit_price=4000, purchased_at=self.start - timedelta(days=2),
        )
        reservation = self.add_member()
        old.refresh_from_db(); new.refresh_from_db(); self.member_b.refresh_from_db()
        consumption = TicketConsumption.objects.get(reservation=reservation)
        ledger = TicketLedger.objects.get(reservation=reservation)
        self.assertEqual((old.remaining_tickets, new.remaining_tickets), (0, 4))
        self.assertEqual(
            (consumption.user_id, consumption.reservation_id, consumption.purchase_id,
             consumption.tickets_used, consumption.unit_price_snapshot),
            (self.member_b.pk, reservation.pk, old.pk, 1, 3500),
        )
        self.assertEqual((ledger.reason, ledger.change_amount), (TicketLedger.REASON_RESERVATION_USE, -1))
        self.assertEqual(reservation.participant_ticket_price_snapshot, 3500)
        self.assertEqual(self.member_b.ticket_balance, 0)

    def test_accounting_matches_normal_reservation_path(self):
        normal_member = User.objects.create_user(
            username="normal-path-member", role=User.ROLE_MEMBER,
            member_level=User.LEVEL_ADVANCED, ticket_balance=1,
        )
        TicketPurchase.objects.create(
            user=normal_member, total_tickets=1, remaining_tickets=1,
            unit_price=3500, purchased_at=self.start - timedelta(days=1),
        )
        normal = Reservation.objects.create(
            user=normal_member, coach=self.coach, court=self.court,
            availability=self.availability, lesson_type=self.availability.lesson_type,
            target_level=self.availability.target_level,
            start_at=self.availability.start_at, end_at=self.availability.end_at,
            status=Reservation.STATUS_ACTIVE,
        )
        normal.consume_tickets(created_by=normal_member)
        post = self.add_member()
        normal.refresh_from_db(); post.refresh_from_db()
        normal_member.refresh_from_db(); self.member_b.refresh_from_db()

        self.assertEqual(normal.tickets_used, post.tickets_used)
        self.assertEqual(
            normal.ticket_consumptions.filter(refunded_at__isnull=True).aggregate(
                total=models.Sum("tickets_used")
            )["total"],
            post.ticket_consumptions.filter(refunded_at__isnull=True).aggregate(
                total=models.Sum("tickets_used")
            )["total"],
        )
        self.assertEqual(normal.participant_ticket_price_snapshot, post.participant_ticket_price_snapshot)
        self.assertEqual(normal_member.ticket_balance, self.member_b.ticket_balance)
        self.assertEqual(
            TicketLedger.objects.get(reservation=normal).change_amount,
            TicketLedger.objects.get(reservation=post).change_amount,
        )
        result = calculate_monthly_settlement(self.start.year, self.start.month, force=True)
        row = next(item for item in result["coach_rows"] if item["coach"].pk == self.coach.pk)
        self.assertEqual(row["ticket_amount"], 7000)

    def test_add_cancel_readd_round_trip_restores_ticket_and_settlement(self):
        settlement = get_or_create_monthly_settlement(self.start.year, self.start.month)
        save_status(settlement, f"availability:{self.availability.pk}", "held", self.coach)
        before = calculate_monthly_settlement(self.start.year, self.start.month, force=True)
        before_row = next(row for row in before["coach_rows"] if row["coach"].pk == self.coach.pk)
        first = self.add_member()
        added = calculate_monthly_settlement(self.start.year, self.start.month, force=True)
        added_row = next(row for row in added["coach_rows"] if row["coach"].pk == self.coach.pk)
        self.assertEqual(added_row["ticket_amount"] - before_row["ticket_amount"], 3500)
        saved = CoachMonthlySettlement.objects.get(monthly_settlement=settlement, coach=self.coach)
        self.assertEqual(saved.ticket_revenue, added_row["ticket_amount"])
        self.assertIn("lesson_compensation_amount", saved.calculation_snapshot)

        canceled, changed = cancel_reservation_with_settlement(
            first.pk, created_by=self.coach, reason="誤登録", schedule_notification=False,
        )
        self.assertTrue(changed)
        canceled.refresh_from_db(); self.member_b.refresh_from_db()
        purchase = TicketPurchase.objects.get(user=self.member_b)
        consumption = TicketConsumption.objects.get(reservation=first)
        restored = calculate_monthly_settlement(self.start.year, self.start.month, force=True)
        restored_row = next(row for row in restored["coach_rows"] if row["coach"].pk == self.coach.pk)
        self.assertEqual(canceled.status, Reservation.STATUS_CANCELED)
        self.assertIsNotNone(consumption.refunded_at)
        self.assertEqual((purchase.remaining_tickets, self.member_b.ticket_balance), (1, 1))
        self.assertTrue(TicketLedger.objects.filter(
            reservation=first, reason=TicketLedger.REASON_CANCEL_REFUND, change_amount=1,
        ).exists())
        self.assertEqual(restored_row["ticket_amount"], before_row["ticket_amount"])

        second = self.add_member()
        self.assertNotEqual(second.pk, first.pk)
        self.assertEqual(Reservation.objects.filter(
            availability=self.availability, user=self.member_b,
            status=Reservation.STATUS_ACTIVE,
        ).count(), 1)
        self.assertEqual(TicketConsumption.objects.filter(
            reservation=second, refunded_at__isnull=True,
        ).count(), 1)

    def test_post_attendance_is_compatible_with_ticket_burden_change(self):
        self.existing.consume_tickets(created_by=self.coach)
        reservation = self.add_member()
        change_lesson_ticket_burden(
            reservation_payers={self.existing.pk: self.member_a.pk, reservation.pk: self.member_a.pk},
            created_by=self.coach,
        )
        self.member_a.refresh_from_db(); self.member_b.refresh_from_db()
        self.assertEqual(self.member_b.ticket_balance, 1)
        self.assertEqual(
            set(reservation.ticket_consumptions.filter(
                refunded_at__isnull=True
            ).values_list("user_id", flat=True)),
            {self.member_a.pk},
        )
        self.assertTrue(TicketBurdenChange.objects.filter(reservation=reservation).exists())

    def test_fixed_occurrence_and_substitute_coach_use_canonical_links_and_revenue(self):
        substitute = User.objects.create_user(
            username="post-attendance-substitute", role=User.ROLE_CONTRACTOR_COACH,
            contractor_hourly_wage=2000,
        )
        fixed = FixedLesson.objects.create(
            title="事後参加固定", coach=self.coach, court=self.court,
            lesson_type=Reservation.LESSON_GENERAL, target_level=User.LEVEL_ADVANCED,
            start_date=timezone.localdate(), weekday=self.start.weekday(),
            start_hour=self.start.hour, capacity=5, coach_count=1, court_count=1,
            weeks_ahead=1,
        )
        self.availability.fixed_lesson_source = fixed
        self.availability.substitute_coach = substitute
        self.availability.save(update_fields=["fixed_lesson_source", "substitute_coach"])
        settlement = get_or_create_monthly_settlement(self.start.year, self.start.month)
        save_status(settlement, f"availability:{self.availability.pk}", "held", self.coach)
        save_status(
            settlement,
            f"fixed:{fixed.pk}:{timezone.localtime(self.start).date().isoformat()}",
            "held",
            self.coach,
        )
        reservation = self.add_member()
        result = calculate_monthly_settlement(self.start.year, self.start.month, force=True)
        substitute_row = next(row for row in result["coach_rows"] if row["coach"].pk == substitute.pk)
        original_row = next(row for row in result["coach_rows"] if row["coach"].pk == self.coach.pk)
        self.assertEqual(reservation.fixed_lesson_id, fixed.pk)
        self.assertEqual(reservation.substitute_coach_id, substitute.pk)
        self.assertEqual(substitute_row["ticket_amount"], 3500)
        self.assertEqual(original_row["ticket_amount"], 0)

    def test_settlement_failure_rolls_back_reservation_and_ticket_changes(self):
        before_balance = self.member_b.ticket_balance
        purchase = TicketPurchase.objects.get(user=self.member_b)
        with patch(
            "club.settlement_service.calculate_monthly_settlement",
            side_effect=RuntimeError("settlement failed"),
        ):
            with self.assertRaisesMessage(RuntimeError, "settlement failed"):
                self.add_member()
        self.member_b.refresh_from_db(); purchase.refresh_from_db()
        self.assertFalse(Reservation.objects.filter(
            availability=self.availability, user=self.member_b,
        ).exists())
        self.assertEqual(self.member_b.ticket_balance, before_balance)
        self.assertEqual(purchase.remaining_tickets, 1)
        self.assertFalse(TicketConsumption.objects.filter(user=self.member_b).exists())
        self.assertFalse(TicketLedger.objects.filter(user=self.member_b).exists())

    def test_zero_balance_uses_deferred_consumption_and_can_be_canceled(self):
        no_ticket = User.objects.create_user(
            username="post-attendance-zero", role=User.ROLE_MEMBER, ticket_balance=0
        )
        reservation = self.add_member(no_ticket)
        pending = TicketConsumption.objects.get(reservation=reservation)
        self.assertIsNone(pending.purchase_id)
        self.assertEqual(pending.tickets_used, 1)

        reservation.cancel(
            created_by=self.coach,
            reason="コーチ/管理者キャンセル",
            schedule_notification=False,
        )
        reservation.refresh_from_db()
        no_ticket.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.STATUS_CANCELED)
        self.assertIsNotNone(pending.refunded_at)
        self.assertEqual(no_ticket.ticket_balance, 0)

    def test_duplicate_full_canceled_and_closed_occurrences_are_rejected(self):
        self.add_member()
        with self.assertRaisesMessage(ValidationError, "すでに"):
            self.add_member()

        extra_members = [
            User.objects.create_user(
                username=f"post-attendance-fill-{index}", role=User.ROLE_MEMBER
            )
            for index in range(3)
        ]
        Reservation.objects.bulk_create([
            Reservation(
                user=member,
                coach=self.coach,
                court=self.court,
                availability=self.availability,
                lesson_type=Reservation.LESSON_GENERAL,
                target_level=User.LEVEL_BEGINNER,
                start_at=self.availability.start_at,
                end_at=self.availability.end_at,
            )
            for member in extra_members
        ])
        third = User.objects.create_user(username="post-attendance-third", role=User.ROLE_MEMBER)
        with self.assertRaisesMessage(ValidationError, "満員"):
            self.add_member(third)

        settlement = get_or_create_monthly_settlement(self.start.year, self.start.month)
        save_status(
            settlement,
            f"availability:{self.availability.pk}",
            "rain_canceled",
            self.coach,
            cancellation_type="rain",
        )
        fourth = User.objects.create_user(username="post-attendance-fourth", role=User.ROLE_MEMBER)
        with self.assertRaisesMessage(ValidationError, "開催予定または実施済み"):
            self.add_member(fourth)

        settlement.status = MonthlySettlement.STATUS_CLOSED
        settlement.save(update_fields=["status"])
        with self.assertRaisesMessage(ValidationError, "締め済み"):
            self.add_member(fourth)

    def test_selected_availability_identity_and_member_ui_are_preserved(self):
        other = CoachAvailability(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_at=self.start,
            end_at=self.start + timedelta(hours=2),
            capacity=5,
        )
        CoachAvailability.objects.bulk_create([other])
        reservation = self.add_member()
        self.assertEqual(reservation.availability_id, self.availability.pk)
        self.assertFalse(Reservation.objects.filter(availability=other).exists())

        self.client.force_login(self.coach)
        response = self.client.get(
            reverse("club:lesson_calendar_member_list"),
            {"availability_id": self.availability.pk},
        )
        self.assertContains(response, "会員を選択")
        self.assertContains(response, "会員追加")
        self.assertContains(response, "チケット負担変更")
        self.assertContains(response, self.member_b.display_name())
        self.assertEqual(
            {member.pk for member in response.context["member_options"]},
            {self.member_a.pk, self.member_b.pk},
        )

    def test_held_previous_day_occurrence_accepts_post_attendance(self):
        past_start = (timezone.localtime(timezone.now()) - timedelta(days=1)).replace(
            hour=19, minute=0, second=0, microsecond=0
        )
        past = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_ADVANCED,
            start_at=past_start,
            end_at=past_start + timedelta(hours=2),
            capacity=5,
        )
        settlement = get_or_create_monthly_settlement(past_start.year, past_start.month)
        save_status(settlement, f"availability:{past.pk}", "held", self.coach)
        attended = User.objects.create_user(
            username="post-attendance-held", role=User.ROLE_MEMBER, ticket_balance=1
        )
        TicketPurchase.objects.create(
            user=attended,
            total_tickets=1,
            remaining_tickets=1,
            unit_price=3500,
            purchased_at=past_start - timedelta(days=1),
        )

        reservation = self.add_member(attended, past)

        self.assertEqual(reservation.availability_id, past.pk)
        self.assertEqual(reservation.tickets_used, 1)
        self.assertIsNotNone(reservation.ticket_consumed_at)
