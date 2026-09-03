from datetime import datetime, time, timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.template import Context, Template
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from club.models import Court, Reservation, StringingOrder
from club.templatetags.dashboard_tags import dashboard_navigation_data


class DashboardNavigationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.member = User.objects.create_user(
            username="nav-member", role=User.ROLE_MEMBER, ticket_balance=2
        )
        self.coach = User.objects.create_user(username="nav-coach", role=User.ROLE_COACH)
        self.contractor = User.objects.create_user(
            username="nav-contractor", role=User.ROLE_CONTRACTOR_COACH
        )
        self.substitute = User.objects.create_user(username="nav-substitute", role=User.ROLE_COACH)
        self.other_coach = User.objects.create_user(username="nav-other-coach", role=User.ROLE_COACH)
        self.admin = User.objects.create_user(username="nav-admin", role=User.ROLE_MEMBER, is_staff=True)
        self.court = Court.objects.create(name="Navigation test court")
        self._reservation_offset = 1

    def reservation_start_at(self, day_offset):
        reservation_date = timezone.localdate() + timedelta(days=day_offset)
        return timezone.make_aware(datetime.combine(reservation_date, time(12)))

    def reservation(self, *, user=None, coach=None, substitute_coach=None, status=Reservation.STATUS_ACTIVE,
                    lesson_type=Reservation.LESSON_PRIVATE, start_at=None):
        if start_at is None:
            start_at = self.reservation_start_at(self._reservation_offset)
            self._reservation_offset += 1
        return Reservation.objects.create(
            user=user or self.member,
            coach=coach or self.coach,
            substitute_coach=substitute_coach,
            court=self.court,
            status=status,
            lesson_type=lesson_type,
            start_at=start_at,
            end_at=start_at + timedelta(
                hours=2 if lesson_type == Reservation.LESSON_GENERAL else 1
            ),
        )

    def test_member_counts_future_capacity_consuming_reservations_in_one_query(self):
        self.reservation(status=Reservation.STATUS_PENDING)
        self.reservation(status=Reservation.STATUS_ACTIVE)
        self.reservation(status=Reservation.STATUS_CANCELED)
        past = self.reservation_start_at(-1)
        self.reservation(status=Reservation.STATUS_PENDING, start_at=past)

        with CaptureQueriesContext(connection) as queries:
            data = dashboard_navigation_data(self.member)

        reservation_queries = [q for q in queries if "club_reservation" in q["sql"].lower()]
        self.assertEqual(data["member_pending_count"], 1)
        self.assertEqual(data["member_upcoming_count"], 2)
        self.assertTrue(data["low_ticket_warning"])
        self.assertEqual(len(reservation_queries), 1)

    def test_member_zero_pending_and_ticket_warning_false_are_preserved(self):
        self.member.ticket_balance = 3
        self.reservation(status=Reservation.STATUS_ACTIVE)

        data = dashboard_navigation_data(self.member)

        self.assertEqual(data["member_pending_count"], 0)
        self.assertEqual(data["member_upcoming_count"], 1)
        self.assertFalse(data["low_ticket_warning"])

    def test_coach_scope_includes_primary_and_substitute_but_excludes_other_coach(self):
        self.reservation(coach=self.coach, status=Reservation.STATUS_PENDING)
        self.reservation(coach=self.other_coach, substitute_coach=self.coach, status=Reservation.STATUS_PENDING)
        self.reservation(coach=self.other_coach, status=Reservation.STATUS_PENDING)
        self.reservation(coach=self.coach, status=Reservation.STATUS_ACTIVE)

        data = dashboard_navigation_data(self.coach)

        self.assertEqual(data["coach_pending_count"], 2)

    def test_staff_sees_all_pending_private_and_group_requests(self):
        self.reservation(coach=self.coach, status=Reservation.STATUS_PENDING)
        self.reservation(coach=self.other_coach, status=Reservation.STATUS_PENDING,
                         lesson_type=Reservation.LESSON_GROUP)
        start_at = self.reservation_start_at(10)
        Reservation.objects.bulk_create([Reservation(
            user=self.member,
            coach=self.other_coach,
            court=self.court,
            status=Reservation.STATUS_PENDING,
            lesson_type=Reservation.LESSON_GENERAL,
            start_at=start_at,
            end_at=start_at + timedelta(hours=2),
        )])

        self.assertEqual(dashboard_navigation_data(self.admin)["coach_pending_count"], 2)

    def test_contractor_coach_keeps_personal_reservation_scope(self):
        self.reservation(coach=self.contractor, status=Reservation.STATUS_PENDING)
        self.reservation(coach=self.other_coach, status=Reservation.STATUS_PENDING)

        self.assertEqual(dashboard_navigation_data(self.contractor)["coach_pending_count"], 1)

    def test_stringing_counts_open_statuses_for_assigned_coach_only(self):
        for status in (StringingOrder.STATUS_REQUESTED, StringingOrder.STATUS_IN_PROGRESS,
                       StringingOrder.STATUS_COMPLETED):
            StringingOrder.objects.create(user=self.member, assigned_coach=self.coach, status=status)
        StringingOrder.objects.create(
            user=self.member, assigned_coach=self.other_coach, status=StringingOrder.STATUS_REQUESTED
        )

        with CaptureQueriesContext(connection) as queries:
            coach_data = dashboard_navigation_data(self.coach)

        self.assertEqual(coach_data["coach_stringing_count"], 2)
        self.assertEqual(dashboard_navigation_data(self.admin)["coach_stringing_count"], 3)
        navigation_queries = [
            q for q in queries
            if "club_reservation" in q["sql"].lower() or "club_stringingorder" in q["sql"].lower()
        ]
        self.assertEqual(len(navigation_queries), 2)

    def test_anonymous_navigation_performs_no_queries(self):
        template = Template("{% load dashboard_tags %}{% dashboard_navigation_data user as nav %}{{ nav.member_upcoming_count }}")
        with self.assertNumQueries(0):
            rendered = template.render(Context({"user": AnonymousUser()}))
        self.assertEqual(rendered, "0")

    def test_base_template_keeps_navigation_labels(self):
        source = Path("club/templates/base.html").read_text(encoding="utf-8")
        self.assertIn("予約確認", source)
        self.assertIn("予約・承認", source)
        self.assertIn("ガット張り", source)
        self.assertIn("dashboard_navigation_data user as nav_data", source)
