from datetime import datetime, timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.models import CoachAvailability, Court, ParticipantPriceChange, Reservation, TicketPurchase, User
from club.participant_accounting import add_guest, cancel_guest, change_participation_amount, participation_revenue


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
