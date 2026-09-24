from datetime import datetime, timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from club.lesson_execution_storage import save_status
from club.models import CoachAvailability, Court, ParticipantPriceChange, Reservation, TicketPurchase, User
from club.participant_accounting import add_guest, cancel_guest, change_participation_amount, participation_revenue
from club.settlement_models import CoachMonthlySettlement, MonthlySettlement
from club.settlement_service import get_or_create_monthly_settlement


class GuestParticipantAccountingTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(username="guest-coach", role=User.ROLE_COACH)
        self.member = User.objects.create_user(username="priced-member", role=User.ROLE_MEMBER)
        self.court = Court.objects.create(name="guest-court", is_active=True)
        start = timezone.make_aware(datetime(2026, 8, 2, 10, 0))
        self.availability = CoachAvailability.objects.create(
            coach=self.coach, court=self.court, lesson_type=Reservation.LESSON_EVENT,
            target_level=User.LEVEL_BEGINNER, start_at=start,
            end_at=start + timedelta(hours=1), capacity=3,
            custom_duration_hours=1,
            status=CoachAvailability.STATUS_OPEN,
        )

    def add_guest(self, name="佐藤花子", amount=1000):
        return add_guest(
            actor=self.coach, guest_name=name, coach=self.coach, court=self.court,
            availability=self.availability, start_at=self.availability.start_at,
            end_at=self.availability.end_at, lesson_type=Reservation.LESSON_EVENT,
            target_level=User.LEVEL_BEGINNER, amount=amount, capacity=3,
        )

    def mark_held(self, availability=None):
        availability = availability or self.availability
        local_start = timezone.localtime(availability.start_at)
        settlement = get_or_create_monthly_settlement(local_start.year, local_start.month)
        save_status(settlement, f"availability:{availability.pk}", "held", self.coach)
        return settlement

    def coach_revenue(self, settlement, coach=None):
        return CoachMonthlySettlement.objects.get(
            monthly_settlement=settlement,
            coach=coach or self.coach,
        ).ticket_revenue

    def test_guest_is_reservation_without_user_and_counts_with_member(self):
        Reservation.objects.create(
            user=self.member, coach=self.coach, court=self.court,
            availability=self.availability, start_at=self.availability.start_at,
            end_at=self.availability.end_at, lesson_type=Reservation.LESSON_EVENT,
            target_level=User.LEVEL_BEGINNER, status=Reservation.STATUS_ACTIVE,
        )
        before = User.objects.count()
        guest = self.add_guest()
        self.assertIsNone(guest.user_id)
        self.assertEqual(guest.guest_name, "佐藤花子")
        self.assertEqual(User.objects.count(), before)
        self.assertEqual(Reservation.objects.filter(availability=self.availability, status=Reservation.STATUS_ACTIVE).count(), 2)

    def test_amount_change_updates_participation_revenue_not_purchase(self):
        purchase = TicketPurchase.objects.create(
            user=self.member, total_tickets=1, remaining_tickets=1, unit_price=1000,
        )
        reservation = Reservation.objects.create(
            user=self.member, coach=self.coach, court=self.court,
            availability=self.availability, start_at=self.availability.start_at,
            end_at=self.availability.end_at, lesson_type=Reservation.LESSON_EVENT,
            target_level=User.LEVEL_BEGINNER, status=Reservation.STATUS_ACTIVE,
            tickets_used=1, participant_ticket_price_snapshot=1000,
        )
        change_participation_amount(reservation_id=reservation.pk, amount=800, actor=self.coach)
        reservation.refresh_from_db(); purchase.refresh_from_db()
        self.assertEqual(reservation.participant_ticket_price_snapshot, 800)
        self.assertEqual(participation_revenue(reservation), 800)
        self.assertEqual(purchase.unit_price, 1000)
        change = ParticipantPriceChange.objects.get(reservation=reservation)
        self.assertEqual(change.participant_name, self.member.display_name())
        self.assertEqual(change.old_amount, 1000)
        self.assertEqual(change.new_amount, 800)

    def test_unset_amount_remains_unknown_until_manual_change(self):
        reservation = Reservation.objects.create(
            user=self.member, coach=self.coach, court=self.court,
            availability=self.availability, start_at=self.availability.start_at,
            end_at=self.availability.end_at, lesson_type=Reservation.LESSON_EVENT,
            target_level=User.LEVEL_BEGINNER, status=Reservation.STATUS_ACTIVE,
            tickets_used=1, participant_ticket_price_snapshot=None,
        )
        self.assertIsNone(participation_revenue(reservation))

        change_participation_amount(
            reservation_id=reservation.pk, amount=3500, actor=self.coach,
        )

        reservation.refresh_from_db()
        change = ParticipantPriceChange.objects.get(reservation=reservation)
        self.assertEqual(reservation.participant_ticket_price_snapshot, 3500)
        self.assertIsNone(change.old_amount)
        self.assertEqual(change.new_amount, 3500)

    def test_explicit_zero_is_formal_revenue_value(self):
        guest = self.add_guest(amount=0)
        self.assertEqual(participation_revenue(guest), 0)

    def test_member_list_distinguishes_unset_zero_and_paid_amounts(self):
        for index, amount in enumerate((None, 0, 3500)):
            Reservation.objects.create(
                user=None, guest_name=f"表示確認{index}",
                coach=self.coach, court=self.court,
                availability=self.availability,
                start_at=self.availability.start_at,
                end_at=self.availability.end_at,
                lesson_type=Reservation.LESSON_EVENT,
                target_level=User.LEVEL_BEGINNER,
                status=Reservation.STATUS_ACTIVE,
                tickets_used=1,
                participant_ticket_price_snapshot=amount,
            )

        self.client.force_login(self.coach)
        response = self.client.get(
            reverse("club:lesson_calendar_member_list"),
            {"availability_id": self.availability.pk},
        )

        self.assertContains(response, "会計金額：未確定")
        self.assertContains(response, 'value="0"', html=False)
        self.assertContains(response, 'value="3500"', html=False)

    def test_zero_and_arbitrary_amount_allowed_negative_rejected(self):
        guest = self.add_guest(amount=0)
        self.assertEqual(guest.participant_ticket_price_snapshot, 0)
        change_participation_amount(reservation_id=guest.pk, amount=2345, actor=self.coach)
        guest.refresh_from_db(); self.assertEqual(guest.participant_ticket_price_snapshot, 2345)
        self.assertEqual(participation_revenue(guest), 2345)
        change = ParticipantPriceChange.objects.filter(reservation=guest).latest("pk")
        self.assertEqual(change.participant_name, "ゲスト：佐藤花子")
        self.assertEqual(change.old_amount, 0)
        self.assertEqual(change.new_amount, 2345)
        with self.assertRaises(ValidationError):
            change_participation_amount(reservation_id=guest.pk, amount=-1, actor=self.coach)

    def test_amount_change_locks_only_reservation_without_nullable_user_join(self):
        queryset = Reservation.objects.select_for_update(of=("self",))
        self.assertEqual(queryset.query.select_for_update_of, ("self",))
        self.assertFalse(queryset.query.select_related)

    def test_cancel_guest_excludes_count_and_revenue_without_delete(self):
        guest = self.add_guest(amount=2000)
        cancel_guest(reservation_id=guest.pk)
        guest.refresh_from_db()
        self.assertEqual(guest.status, Reservation.STATUS_CANCELED)
        self.assertEqual(participation_revenue(guest), 0)
        self.assertTrue(Reservation.objects.filter(pk=guest.pk).exists())

    def test_capacity_and_multiple_guests(self):
        self.add_guest("一人目", 500); self.add_guest("二人目", 1000); self.add_guest("三人目", 0)
        with self.assertRaises(ValidationError):
            self.add_guest("超過", 1000)

    def test_held_guest_add_change_and_cancel_keep_settlement_synchronized(self):
        settlement = self.mark_held()

        guest = self.add_guest(amount=1000)
        self.assertEqual(self.coach_revenue(settlement), 1000)

        change_participation_amount(
            reservation_id=guest.pk, amount=800, actor=self.coach,
        )
        self.assertEqual(self.coach_revenue(settlement), 800)

        cancel_guest(reservation_id=guest.pk)
        self.assertEqual(self.coach_revenue(settlement), 0)

    def test_same_amount_and_repeated_cancel_are_no_ops(self):
        guest = self.add_guest(amount=1000)
        initial_changes = ParticipantPriceChange.objects.filter(reservation=guest).count()
        with patch("club.settlement_service.calculate_monthly_settlement") as calculate:
            change_participation_amount(
                reservation_id=guest.pk, amount=1000, actor=self.coach,
            )
        self.assertEqual(
            ParticipantPriceChange.objects.filter(reservation=guest).count(),
            initial_changes,
        )
        calculate.assert_not_called()

        cancel_guest(reservation_id=guest.pk)
        with patch("club.settlement_service.calculate_monthly_settlement") as calculate:
            cancel_guest(reservation_id=guest.pk)
        calculate.assert_not_called()

    def test_future_and_unconfirmed_guests_are_not_forced_into_revenue(self):
        future_start = (timezone.localtime(timezone.now()) + timedelta(days=40)).replace(
            hour=10, minute=0, second=0, microsecond=0,
        )
        future = CoachAvailability.objects.create(
            coach=self.coach, court=self.court, lesson_type=Reservation.LESSON_EVENT,
            target_level=User.LEVEL_BEGINNER, start_at=future_start,
            end_at=future_start + timedelta(hours=1), capacity=3,
            custom_duration_hours=1, status=CoachAvailability.STATUS_OPEN,
        )
        future_guest = add_guest(
            actor=self.coach, guest_name="未来ゲスト", coach=self.coach,
            court=self.court, availability=future, start_at=future.start_at,
            end_at=future.end_at, lesson_type=Reservation.LESSON_EVENT,
            target_level=User.LEVEL_BEGINNER, amount=1000, capacity=3,
        )
        future_local = timezone.localtime(future.start_at)
        future_settlement = MonthlySettlement.objects.get(
            year=future_local.year, month=future_local.month,
        )
        self.assertEqual(future_guest.participant_ticket_price_snapshot, 1000)
        self.assertFalse(CoachMonthlySettlement.objects.filter(
            monthly_settlement=future_settlement,
            coach=self.coach,
            ticket_revenue__gt=0,
        ).exists())

        unconfirmed_guest = self.add_guest(name="確認待ちゲスト", amount=1200)
        local_start = timezone.localtime(self.availability.start_at)
        unconfirmed_settlement = MonthlySettlement.objects.get(
            year=local_start.year, month=local_start.month,
        )
        self.assertEqual(unconfirmed_guest.participant_ticket_price_snapshot, 1200)
        self.assertFalse(CoachMonthlySettlement.objects.filter(
            monthly_settlement=unconfirmed_settlement,
            coach=self.coach,
            ticket_revenue__gt=0,
        ).exists())

    def test_substitute_coach_receives_held_guest_revenue(self):
        substitute = User.objects.create_user(
            username="guest-substitute", role=User.ROLE_CONTRACTOR_COACH,
            contractor_hourly_wage=2000,
        )
        self.availability.substitute_coach = substitute
        self.availability.save(update_fields=["substitute_coach"])
        settlement = self.mark_held()

        self.add_guest(amount=1000)

        self.assertEqual(self.coach_revenue(settlement, substitute), 1000)
        self.assertEqual(self.coach_revenue(settlement, self.coach), 0)

    def test_closed_month_rejects_guest_accounting_changes(self):
        settlement = get_or_create_monthly_settlement(2026, 8)
        guest_to_change = self.add_guest(name="金額変更対象", amount=1000)
        guest_to_cancel = self.add_guest(name="取消対象", amount=1000)
        settlement.status = MonthlySettlement.STATUS_CLOSED
        settlement.save(update_fields=["status"])

        with self.assertRaises(ValidationError):
            self.add_guest(name="追加拒否", amount=1000)
        with self.assertRaises(ValidationError):
            change_participation_amount(
                reservation_id=guest_to_change.pk, amount=800, actor=self.coach,
            )
        with self.assertRaises(ValidationError):
            cancel_guest(reservation_id=guest_to_cancel.pk)

        guest_to_change.refresh_from_db()
        guest_to_cancel.refresh_from_db()
        self.assertEqual(guest_to_change.participant_ticket_price_snapshot, 1000)
        self.assertEqual(guest_to_cancel.status, Reservation.STATUS_ACTIVE)

    @override_settings(TIME_ZONE="America/Los_Angeles")
    def test_closed_month_uses_local_date_at_month_boundary(self):
        start = timezone.make_aware(datetime(2026, 9, 30, 20, 0))
        availability = CoachAvailability.objects.create(
            coach=self.coach, court=self.court, lesson_type=Reservation.LESSON_EVENT,
            target_level=User.LEVEL_BEGINNER, start_at=start,
            end_at=start + timedelta(hours=1), capacity=3,
            custom_duration_hours=1, status=CoachAvailability.STATUS_OPEN,
        )
        MonthlySettlement.objects.create(
            year=2026, month=9, status=MonthlySettlement.STATUS_CLOSED,
        )

        with self.assertRaises(ValidationError):
            add_guest(
                actor=self.coach, guest_name="月境界ゲスト", coach=self.coach,
                court=self.court, availability=availability,
                start_at=availability.start_at, end_at=availability.end_at,
                lesson_type=Reservation.LESSON_EVENT,
                target_level=User.LEVEL_BEGINNER, amount=1000, capacity=3,
            )

    def test_settlement_failure_rolls_back_each_guest_change(self):
        guest_to_change = self.add_guest(name="金額rollback", amount=1000)
        guest_to_cancel = self.add_guest(name="取消rollback", amount=1000)
        initial_change_count = ParticipantPriceChange.objects.count()

        with patch(
            "club.settlement_service.calculate_monthly_settlement",
            side_effect=RuntimeError("settlement failed"),
        ):
            with self.assertRaisesMessage(RuntimeError, "settlement failed"):
                self.add_guest(name="追加rollback", amount=1000)
            with self.assertRaisesMessage(RuntimeError, "settlement failed"):
                change_participation_amount(
                    reservation_id=guest_to_change.pk, amount=800, actor=self.coach,
                )
            with self.assertRaisesMessage(RuntimeError, "settlement failed"):
                cancel_guest(reservation_id=guest_to_cancel.pk)

        guest_to_change.refresh_from_db()
        guest_to_cancel.refresh_from_db()
        self.assertFalse(Reservation.objects.filter(guest_name="追加rollback").exists())
        self.assertEqual(guest_to_change.participant_ticket_price_snapshot, 1000)
        self.assertEqual(guest_to_cancel.status, Reservation.STATUS_ACTIVE)
        self.assertEqual(ParticipantPriceChange.objects.count(), initial_change_count)
