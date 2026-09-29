from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.forms import CoachAvailabilityForm
from club.models import (
    CoachAvailability,
    Court,
    Reservation,
    TicketConsumption,
    TicketLedger,
    TicketPurchase,
    User,
)


class CalendarGroupLessonCreationTests(TestCase):
    def setUp(self):
        self.target_date = timezone.localdate() + timedelta(days=5)
        self.coach = User.objects.create_user(
            username="group-coach", password="password", role=User.ROLE_COACH
        )
        self.member1 = User.objects.create_user(
            username="group-member-1", password="password", role=User.ROLE_MEMBER,
            member_level=User.LEVEL_BEGINNER,
        )
        self.member2 = User.objects.create_user(
            username="group-member-2", password="password", role=User.ROLE_MEMBER,
            member_level=User.LEVEL_BEGINNER,
        )
        self.member3 = User.objects.create_user(
            username="group-member-3", password="password", role=User.ROLE_MEMBER,
            member_level=User.LEVEL_BEGINNER,
        )
        self.court = Court.objects.create(name="Group creation court", available_court_count=2)

    def data(self, *, count=2, end_hour=10, rate=1, members=None):
        members = members or [self.member1, self.member2]
        data = {
            "source": "calendar",
            "start_date": self.target_date.isoformat(),
            "start_hour": "9",
            "end_date": self.target_date.isoformat(),
            "end_hour": str(end_hour),
            "coach": self.coach.pk,
            "coach_2": "",
            "substitute_coach": "",
            "court": self.court.pk,
            "lesson_type": Reservation.LESSON_GROUP,
            "target_level": User.LEVEL_BEGINNER,
            "target_level_2": "",
            "coach_count": 1,
            "court_count": 1,
            "capacity": count,
            "custom_ticket_price": 0,
            "custom_duration_hours": 0,
            "customer_count": str(count),
            "tickets_per_person_per_hour": str(rate),
            "note": "",
        }
        for index, member in enumerate(members, 1):
            data[f"member_{index}"] = member.pk
        return data

    def test_calendar_opens_group_form_and_member_is_forbidden(self):
        self.client.force_login(self.coach)
        calendar = self.client.get(
            reverse("club:lesson_calendar"),
            {"year": self.target_date.year, "month": self.target_date.month},
        )
        self.assertContains(calendar, "＋ レッスン作成")
        self.assertNotContains(calendar, "＋ 一般レッスン")
        self.assertNotContains(calendar, "＋ グループレッスン")
        response = self.client.get(
            reverse("club:coach_availability_create"),
            {"date": self.target_date.isoformat(), "source": "calendar"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["start_date"].value(), self.target_date)
        self.client.force_login(self.member1)
        self.assertEqual(self.client.get(reverse("club:coach_availability_create")).status_code, 403)

    def test_count_requires_exact_distinct_members(self):
        form = CoachAvailabilityForm(
            data=self.data(count=2, members=[self.member1]), request_user=self.coach,
            calendar_creation=True,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("顧客人数と選択メンバー数", str(form.errors))
        duplicate = CoachAvailabilityForm(
            data=self.data(members=[self.member1, self.member1]), request_user=self.coach,
            calendar_creation=True,
        )
        self.assertFalse(duplicate.is_valid())
        self.assertIn("同じ会員", str(duplicate.errors))

    def test_explicit_rate_is_snapshotted_per_reservation(self):
        self.client.force_login(self.coach)
        cases = ((2, 10, 1, 1, 2), (2, 11, 1, 2, 4), (3, 10, 2, 2, 6))
        for count, end_hour, rate, expected_each, expected_total in cases:
            with self.subTest(count=count, end_hour=end_hour, rate=rate):
                CoachAvailability.objects.all().delete()
                User.objects.filter(
                    pk__in=[self.member1.pk, self.member2.pk, self.member3.pk]
                ).update(ticket_balance=0)
                members = [self.member1, self.member2, self.member3][:count]
                response = self.client.post(
                    reverse("club:coach_availability_create"),
                    self.data(count=count, end_hour=end_hour, rate=rate, members=members),
                )
                self.assertEqual(response.status_code, 302)
                availability = CoachAvailability.objects.get()
                reservations = list(Reservation.objects.filter(availability=availability))
                self.assertEqual(availability.capacity, count)
                self.assertEqual(availability.group_tickets_per_person_per_hour, rate)
                self.assertEqual([row.tickets_used for row in reservations], [expected_each] * count)
                self.assertEqual(sum(row.tickets_used for row in reservations), expected_total)
                self.assertTrue(all(row.group_tickets_per_person_per_hour == rate for row in reservations))
                Reservation.objects.all().delete()

    def test_zero_rate_creates_no_ticket_accounting(self):
        self.client.force_login(self.coach)
        response = self.client.post(
            reverse("club:coach_availability_create"), self.data(rate=0)
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(set(Reservation.objects.values_list("tickets_used", flat=True)), {0})
        self.assertFalse(TicketConsumption.objects.exists())
        self.assertFalse(TicketLedger.objects.exists())

    def test_second_reservation_failure_rolls_back_everything(self):
        self.client.force_login(self.coach)
        from club import views

        real_create = views.create_reservation
        calls = 0

        def fail_second(**values):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValidationError("second failed")
            return real_create(**values)

        with patch("club.views.create_reservation", side_effect=fail_second):
            response = self.client.post(reverse("club:coach_availability_create"), self.data())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CoachAvailability.objects.exists())
        self.assertFalse(Reservation.objects.exists())
        self.assertFalse(TicketConsumption.objects.exists())
        self.assertFalse(TicketLedger.objects.exists())

    def test_legacy_group_ticket_calculation_remains_unchanged_without_snapshot(self):
        start = timezone.make_aware(
            timezone.datetime.combine(self.target_date, timezone.datetime.min.time()).replace(hour=9)
        )
        reservation = Reservation(
            user=self.member1, coach=self.coach, court=self.court,
            lesson_type=Reservation.LESSON_GROUP, target_level=User.LEVEL_BEGINNER,
            start_at=start, end_at=start + timedelta(hours=1),
        )
        self.assertIsNone(reservation.group_tickets_per_person_per_hour)
        self.assertEqual(reservation.calculate_tickets_used(), 1)

    def test_private_creation_uses_canonical_ticket_consumption(self):
        TicketPurchase.objects.create(
            user=self.member1,
            total_tickets=6,
            remaining_tickets=6,
            unit_price=2000,
            purchased_at=timezone.now() - timedelta(days=1),
        )
        self.member1.ticket_balance = 6
        self.member1.save(update_fields=["ticket_balance"])
        self.client.force_login(self.coach)
        for offset, end_hour, expected_tickets in ((0, 10, 2), (1, 11, 4)):
            with self.subTest(end_hour=end_hour):
                target_date = self.target_date + timedelta(days=offset)
                data = self.data(end_hour=end_hour)
                data.update({
                    "start_date": target_date.isoformat(),
                    "end_date": target_date.isoformat(),
                    "lesson_type": Reservation.LESSON_PRIVATE,
                    "private_member": self.member1.pk,
                })
                response = self.client.post(
                    reverse("club:coach_availability_create"), data
                )
                self.assertEqual(
                    response.status_code,
                    302,
                    getattr(response.context.get("form"), "errors", "") if response.context else "",
                )
                reservation = Reservation.objects.get(start_at__date=target_date)
                self.assertEqual(reservation.user, self.member1)
                self.assertEqual(reservation.tickets_used, expected_tickets)
                self.assertEqual(
                    sum(reservation.ticket_consumptions.values_list("tickets_used", flat=True)),
                    expected_tickets,
                )
