from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from club.family_reservations import save_reservation_participant_snapshot
from club.lesson_participants import CAPACITY_CONSUMING_STATUSES
from club.models import CoachAvailability, Court, FamilyMember, Reservation, User
from club.reservation_service import create_reservation


class CapacityFixtureMixin:
    def make_slot(self, *, capacity):
        court = Court.objects.create(name=f"capacity-court-{capacity}", is_active=True)
        coach = User.objects.create_user(
            username=f"capacity-coach-{capacity}",
            role=User.ROLE_COACH,
            full_name="Capacity Coach",
            member_level=User.LEVEL_ADVANCED,
        )
        start_at = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=7), datetime.min.time()).replace(hour=10)
        )
        availability = CoachAvailability.objects.create(
            coach=coach,
            court=court,
            lesson_type=Reservation.LESSON_EVENT,
            target_level=User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=start_at + timedelta(hours=1),
            capacity=capacity,
            custom_duration_hours=1,
            status=CoachAvailability.STATUS_OPEN,
        )
        return coach, court, availability

    def make_member(self, username):
        return User.objects.create_user(
            username=username,
            role=User.ROLE_MEMBER,
            full_name=username,
            member_level=User.LEVEL_BEGINNER,
            ticket_balance=0,
        )

    def reservation_values(self, member, coach, court, availability, **overrides):
        values = {
            "user": member,
            "coach": coach,
            "court": court,
            "availability": availability,
            "lesson_type": Reservation.LESSON_EVENT,
            "target_level": User.LEVEL_BEGINNER,
            "start_at": availability.start_at,
            "end_at": availability.end_at,
            "status": Reservation.STATUS_ACTIVE,
            "custom_duration_hours": 1,
        }
        values.update(overrides)
        return values


class ReservationCapacityPolicyTests(CapacityFixtureMixin, TestCase):
    def test_pending_consumes_capacity_and_cancel_releases_it(self):
        coach, court, availability = self.make_slot(capacity=1)
        first = self.make_member("pending-capacity-first")
        second = self.make_member("pending-capacity-second")
        pending = create_reservation(
            **self.reservation_values(
                first, coach, court, availability, status=Reservation.STATUS_PENDING
            )
        )

        with self.assertRaises(ValidationError):
            create_reservation(**self.reservation_values(second, coach, court, availability))

        pending.cancel(reason="capacity release test", schedule_notification=False)
        created = create_reservation(**self.reservation_values(second, coach, court, availability))
        self.assertEqual(created.status, Reservation.STATUS_ACTIVE)

    def test_rain_canceled_does_not_consume_capacity(self):
        coach, court, availability = self.make_slot(capacity=1)
        historical = self.make_member("rain-capacity-first")
        replacement = self.make_member("rain-capacity-second")
        Reservation.objects.create(
            **self.reservation_values(
                historical, coach, court, availability, status=Reservation.STATUS_RAIN_CANCELED
            )
        )

        created = create_reservation(**self.reservation_values(replacement, coach, court, availability))
        self.assertEqual(created.status, Reservation.STATUS_ACTIVE)

    def test_two_family_participants_are_all_or_nothing(self):
        coach, court, availability = self.make_slot(capacity=1)
        parent = self.make_member("family-capacity-parent")
        children = [
            FamilyMember.objects.create(
                parent=parent,
                full_name="Child One",
                member_level=User.LEVEL_BEGINNER,
                is_active=True,
            ),
            FamilyMember.objects.create(
                parent=parent,
                full_name="Child Two",
                member_level=User.LEVEL_BEGINNER,
                is_active=True,
            ),
        ]

        with self.assertRaises(ValidationError):
            with transaction.atomic():
                for child in children:
                    reservation = create_reservation(
                        **self.reservation_values(parent, coach, court, availability)
                    )
                    save_reservation_participant_snapshot(
                        reservation,
                        {
                            "type": "family",
                            "family_member": child,
                            "family_member_id": child.pk,
                            "name": child.full_name,
                            "level": child.member_level,
                        },
                    )

        self.assertFalse(Reservation.objects.filter(availability=availability).exists())


class ReservationCapacityConcurrencyTests(CapacityFixtureMixin, TransactionTestCase):
    reset_sequences = True

    @skipUnlessDBFeature("has_select_for_update")
    def test_two_simultaneous_writes_to_empty_single_seat_slot(self):
        coach, court, availability = self.make_slot(capacity=1)
        contenders = [self.make_member("single-seat-a"), self.make_member("single-seat-b")]
        barrier = Barrier(2)

        def attempt(member_id):
            close_old_connections()
            try:
                member = User.objects.get(pk=member_id)
                locked_availability = CoachAvailability.objects.get(pk=availability.pk)
                barrier.wait(timeout=10)
                create_reservation(
                    **self.reservation_values(member, coach, court, locked_availability)
                )
                return "created"
            except ValidationError:
                return "full"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, [member.pk for member in contenders]))

        self.assertCountEqual(results, ["created", "full"])
        self.assertEqual(
            Reservation.objects.filter(
                availability=availability,
                status__in=CAPACITY_CONSUMING_STATUSES,
            ).count(),
            1,
        )
        self.assertEqual(connection.vendor, "postgresql")

    @skipUnlessDBFeature("has_select_for_update")
    def test_two_simultaneous_writes_allocate_only_one_remaining_seat(self):
        coach, court, availability = self.make_slot(capacity=5)
        existing_members = [self.make_member(f"capacity-existing-{index}") for index in range(4)]
        contenders = [self.make_member("capacity-contender-a"), self.make_member("capacity-contender-b")]
        for member in existing_members:
            create_reservation(**self.reservation_values(member, coach, court, availability))

        barrier = Barrier(2)

        def attempt(member_id):
            close_old_connections()
            try:
                member = User.objects.get(pk=member_id)
                locked_availability = CoachAvailability.objects.get(pk=availability.pk)
                barrier.wait(timeout=10)
                create_reservation(
                    **self.reservation_values(member, coach, court, locked_availability)
                )
                return "created"
            except ValidationError:
                return "full"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, [member.pk for member in contenders]))

        self.assertCountEqual(results, ["created", "full"])
        self.assertEqual(
            Reservation.objects.filter(
                availability=availability,
                status__in=CAPACITY_CONSUMING_STATUSES,
            ).count(),
            5,
        )
        self.assertEqual(connection.vendor, "postgresql")
