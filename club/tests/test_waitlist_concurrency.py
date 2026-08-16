from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature

from club.models import LessonWaitlist, Reservation
from club.tests.test_reservation_capacity_concurrency import CapacityFixtureMixin
from club.waitlist_service import promote_waitlist


class WaitlistFixtureMixin(CapacityFixtureMixin):
    def make_waitlist(self, username, coach, court, availability):
        member = self.make_member(username)
        return LessonWaitlist.objects.create(
            user=member,
            coach=coach,
            court=court,
            availability=availability,
            lesson_type=Reservation.LESSON_EVENT,
            target_level=member.member_level,
            start_at=availability.start_at,
            end_at=availability.end_at,
        )


class WaitlistPromotionPolicyTests(WaitlistFixtureMixin, TestCase):
    def test_fifo_head_must_be_promoted_first(self):
        coach, court, availability = self.make_slot(capacity=2)
        first = self.make_waitlist("waitlist-fifo-a", coach, court, availability)
        second = self.make_waitlist("waitlist-fifo-b", coach, court, availability)

        with self.assertRaises(ValidationError):
            promote_waitlist(second.pk, created_by=coach)

        promote_waitlist(first.pk, created_by=coach)
        promote_waitlist(second.pk, created_by=coach)
        self.assertEqual(
            list(
                Reservation.objects.filter(availability=availability)
                .order_by("id")
                .values_list("user_id", flat=True)
            ),
            [first.user_id, second.user_id],
        )

    def test_recruitment_closed_does_not_promote(self):
        coach, court, availability = self.make_slot(capacity=1)
        waitlist = self.make_waitlist("waitlist-closed", coach, court, availability)
        availability.is_recruitment_closed = True
        availability.save(update_fields=["is_recruitment_closed"])

        with self.assertRaises(ValidationError):
            promote_waitlist(waitlist.pk, created_by=coach)

        waitlist.refresh_from_db()
        self.assertEqual(waitlist.status, LessonWaitlist.STATUS_WAITING)
        self.assertFalse(Reservation.objects.filter(availability=availability).exists())


class WaitlistPromotionConcurrencyTests(WaitlistFixtureMixin, TransactionTestCase):
    reset_sequences = True

    @skipUnlessDBFeature("has_select_for_update")
    def test_two_workers_cannot_promote_more_than_capacity(self):
        coach, court, availability = self.make_slot(capacity=1)
        waitlists = [
            self.make_waitlist("waitlist-race-a", coach, court, availability),
            self.make_waitlist("waitlist-race-b", coach, court, availability),
        ]
        barrier = Barrier(2)

        def attempt(waitlist_id):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                promote_waitlist(waitlist_id, created_by=coach)
                return "promoted"
            except ValidationError:
                return "rejected"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, [row.pk for row in waitlists]))

        self.assertCountEqual(results, ["promoted", "rejected"])
        self.assertEqual(
            Reservation.objects.filter(availability=availability, status=Reservation.STATUS_ACTIVE).count(),
            1,
        )
        self.assertEqual(
            LessonWaitlist.objects.filter(
                availability=availability, status=LessonWaitlist.STATUS_CONVERTED
            ).count(),
            1,
        )

    @skipUnlessDBFeature("has_select_for_update")
    def test_two_workers_promoting_same_row_create_one_reservation(self):
        coach, court, availability = self.make_slot(capacity=1)
        waitlist = self.make_waitlist("waitlist-same-row", coach, court, availability)
        barrier = Barrier(2)

        def attempt():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                promote_waitlist(waitlist.pk, created_by=coach)
                return "promoted"
            except ValidationError:
                return "rejected"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: attempt(), range(2)))

        self.assertCountEqual(results, ["promoted", "rejected"])
        self.assertEqual(Reservation.objects.filter(availability=availability).count(), 1)
