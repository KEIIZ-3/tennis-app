from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.lesson_execution_storage import save_status
from club.models import (
    CoachAvailability,
    Court,
    Reservation,
    TicketConsumption,
    TicketLedger,
    TicketPurchase,
    User,
)
from club.participant_accounting import add_member_to_lesson_occurrence
from club.settlement_models import MonthlySettlement
from club.settlement_service import get_or_create_monthly_settlement


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
        self.start = (timezone.now() + timedelta(days=2)).replace(
            hour=10, minute=0, second=0, microsecond=0
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
