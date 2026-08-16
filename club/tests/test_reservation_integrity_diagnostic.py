from datetime import date, datetime, time, timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from club.models import CoachAvailability, Court, FixedLesson, Reservation, User
from club.reservation_integrity_diagnostic import diagnose_reservation_integrity


class ReservationIntegrityDiagnosticTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(username="audit-coach", role=User.ROLE_COACH)
        self.members = [
            User.objects.create_user(username=f"audit-member-{index}", role=User.ROLE_MEMBER)
            for index in range(3)
        ]
        self.court = Court.objects.create(name="Audit Court")
        self.target_date = date(2099, 1, 5)
        self.fixed = FixedLesson.objects.create(
            coach=self.coach, court=self.court, start_date=self.target_date,
            weekday=self.target_date.weekday(), start_hour=10, capacity=2,
            lesson_type=FixedLesson.LESSON_GROUP, weeks_ahead=1,
        )

    def reservation(self, member, status=Reservation.STATUS_ACTIVE):
        start_at = timezone.make_aware(datetime.combine(self.target_date, time(10)))
        return Reservation.objects.create(
            user=member, coach=self.coach, court=self.court,
            fixed_lesson=self.fixed, lesson_type=Reservation.LESSON_GROUP,
            start_at=start_at, end_at=start_at + timedelta(hours=1), status=status,
        )

    def historical_reservations(self, members):
        """Insert impossible-through-current-save rows for diagnostic coverage."""
        start_at = timezone.make_aware(datetime.combine(self.target_date, time(10)))
        return Reservation.objects.bulk_create([
            Reservation(
                user=member, coach=self.coach, court=self.court,
                fixed_lesson=self.fixed, lesson_type=Reservation.LESSON_GROUP,
                start_at=start_at, end_at=start_at + timedelta(hours=1),
                status=Reservation.STATUS_ACTIVE,
            )
            for member in members
        ])

    def test_active_rows_are_counted_once_and_canceled_rows_are_excluded(self):
        self.reservation(self.members[0])
        self.reservation(self.members[1], Reservation.STATUS_CANCELED)
        result = diagnose_reservation_integrity(today=date(2099, 1, 1))
        self.assertEqual(result["occurrences"][0]["active_count"], 1)
        self.assertEqual(result["occurrences"][0]["canceled_count"], 1)

    def test_family_account_reservations_are_not_deduplicated(self):
        self.historical_reservations([self.members[0], self.members[0]])
        result = diagnose_reservation_integrity(today=date(2099, 1, 1))
        self.assertEqual(result["occurrences"][0]["active_count"], 2)

    def test_capacity_excess_is_an_error(self):
        self.historical_reservations(self.members)
        result = diagnose_reservation_integrity(today=date(2099, 1, 1))
        self.assertEqual(result["findings"][0]["category"], "J_CAPACITY_EXCEEDED")

    def test_fixed_member_without_reservation_is_detected(self):
        self.fixed.members.add(self.members[0])
        Reservation.objects.filter(fixed_lesson=self.fixed).delete()
        result = diagnose_reservation_integrity(today=date(2099, 1, 1))
        categories = {row["category"] for row in result["findings"]}
        self.assertIn("F_FIXED_MEMBER_WITHOUT_FUTURE_RESERVATION", categories)

    def test_diagnostic_does_not_change_database_state(self):
        reservation = self.reservation(self.members[0])
        before = list(Reservation.objects.values().order_by("id"))
        diagnose_reservation_integrity(today=date(2099, 1, 1))
        self.assertEqual(list(Reservation.objects.values().order_by("id")), before)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.STATUS_ACTIVE)

    def test_split_fixed_lesson_links_on_one_availability_are_detected(self):
        start_at = timezone.make_aware(datetime.combine(self.target_date, time(10)))
        availability = CoachAvailability.objects.create(
            coach=self.coach, court=self.court, lesson_type=Reservation.LESSON_GROUP,
            start_at=start_at, end_at=start_at + timedelta(hours=1), capacity=2,
        )
        replacement = FixedLesson.objects.create(
            coach=self.coach, court=self.court, start_date=self.target_date,
            weekday=self.target_date.weekday(), start_hour=10, capacity=2,
            lesson_type=FixedLesson.LESSON_GROUP, weeks_ahead=1,
        )
        for member, fixed in zip(self.members[:2], [self.fixed, replacement]):
            Reservation.objects.create(
                user=member, coach=self.coach, court=self.court,
                availability=availability, fixed_lesson=fixed,
                lesson_type=Reservation.LESSON_GROUP, start_at=start_at,
                end_at=start_at + timedelta(hours=1), status=Reservation.STATUS_ACTIVE,
            )

        result = diagnose_reservation_integrity(today=date(2099, 1, 1))
        finding = next(row for row in result["findings"] if row["category"] == "OCCURRENCE_LINK_MISMATCH")
        self.assertEqual(finding["severity"], "ERROR")
        self.assertEqual(finding["availability_id"], availability.pk)
        occurrence = next(row for row in result["occurrences"] if row["occurrence"] == f"availability:{availability.pk}")
        self.assertEqual(occurrence["active_count"], 2)

    def test_repair_command_is_dry_run_by_default_and_idempotent_when_applied(self):
        start_at = timezone.make_aware(datetime.combine(self.target_date, time(10)))
        availability = CoachAvailability.objects.create(
            coach=self.coach, court=self.court, lesson_type=Reservation.LESSON_GROUP,
            start_at=start_at, end_at=start_at + timedelta(hours=1), capacity=2,
        )
        replacement = FixedLesson.objects.create(
            title=self.fixed.title, coach=self.coach, court=self.court,
            start_date=self.target_date, weekday=self.target_date.weekday(),
            start_hour=10, capacity=2, lesson_type=FixedLesson.LESSON_GROUP,
            weeks_ahead=1,
        )
        reservation = Reservation.objects.create(
            user=self.members[0], coach=self.coach, court=self.court,
            availability=availability, fixed_lesson=self.fixed,
            lesson_type=Reservation.LESSON_GROUP, start_at=start_at,
            end_at=start_at + timedelta(hours=1), status=Reservation.STATUS_ACTIVE,
            tickets_used=1, ticket_consumed_at=timezone.now(),
        )
        evidence = (reservation.pk, reservation.tickets_used, reservation.ticket_consumed_at)
        args = ["repair_occurrence_linkage", "--reservation-id", str(reservation.pk),
                "--canonical-fixed-lesson-id", str(replacement.pk)]
        call_command(*args, stdout=StringIO())
        reservation.refresh_from_db()
        self.assertEqual(reservation.fixed_lesson_id, self.fixed.pk)

        call_command(*args, "--apply", stdout=StringIO())
        call_command(*args, "--apply", stdout=StringIO())
        reservation.refresh_from_db()
        self.assertEqual(reservation.fixed_lesson_id, replacement.pk)
        self.assertEqual((reservation.pk, reservation.tickets_used, reservation.ticket_consumed_at), evidence)
