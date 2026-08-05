from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.settlement_integrity_diagnostic import (
    affected_closed_settlements,
    _participant_counts,
    diagnose_closed_settlement,
)
from club.settlement_models import (
    CoachMonthlySettlement,
    MonthlySettlement,
    SettlementPayment,
)
from club.settlement_balance_policy import _month_range


def _coach(pk, name):
    return SimpleNamespace(
        pk=pk, role="coach", display_name=lambda: name
    )


def _reservation(pk, coach, start_at, status_key, user_id=None):
    return SimpleNamespace(
        pk=pk,
        user_id=user_id or pk,
        start_at=start_at,
        end_at=start_at + timedelta(hours=1),
        fixed_lesson=None,
        availability=SimpleNamespace(pk=status_key),
        availability_id=status_key,
        substitute_coach=None,
        coach=coach,
        assigned_coach=lambda: coach,
    )


class ParticipantCountComparisonTests(TestCase):
    def setUp(self):
        self.coach_1 = _coach(1, "コーチ1")
        self.coach_2 = _coach(2, "コーチ2")
        self.start = timezone.make_aware(datetime(2025, 12, 31, 20))

    def test_legacy_includes_held_and_scheduled_but_current_only_held(self):
        rows = [
            _reservation(1, self.coach_1, self.start, 10),
            _reservation(2, self.coach_1, self.start, 11),
            _reservation(3, self.coach_2, self.start, 12),
        ]
        statuses = {
            "availability:10": {"status": "held"},
            "availability:11": {"status": "scheduled"},
            "availability:12": {"status": "rain_canceled"},
        }
        old = _participant_counts(rows, statuses, [1, 2], {"held", "scheduled"})
        current = _participant_counts(rows, statuses, [1, 2], {"held"})
        self.assertEqual(old, {1: 2})
        self.assertEqual(current, {1: 1})

    def test_duplicate_participants_are_counted_once_per_occurrence(self):
        rows = [
            _reservation(1, self.coach_1, self.start, 10, user_id=99),
            _reservation(2, self.coach_1, self.start, 10, user_id=99),
        ]
        statuses = {"availability:10": {"status": "held"}}
        self.assertEqual(
            _participant_counts(rows, statuses, [1], {"held"}), {1: 1}
        )

    def test_month_range_handles_year_boundary(self):
        self.assertEqual(
            _month_range(2025, 12), (date(2025, 12, 1), date(2026, 1, 1))
        )


class ClosedSettlementDiagnosticTests(TestCase):
    def setUp(self):
        self.coaches = [_coach(1, "コーチ1"), _coach(2, "コーチ2")]
        self.settlement = SimpleNamespace(year=2025, month=12)
        self.start = timezone.make_aware(datetime(2025, 12, 31, 20))

    def _saved_rows(self, snapshots):
        queryset = MagicMock()
        queryset.select_related.return_value = [
            SimpleNamespace(coach_id=coach_id, calculation_snapshot=snapshot)
            for coach_id, snapshot in snapshots.items()
        ]
        return queryset

    @patch("club.settlement_integrity_diagnostic.CoachMonthlySettlement.objects.filter")
    @patch("club.settlement_integrity_diagnostic._ball_amount", return_value=101)
    @patch("club.settlement_integrity_diagnostic._monthly_execution_reservations_and_status")
    def test_candidate_compares_ratio_rounding_and_saved_amount(
        self, execution, _amount, saved_filter
    ):
        rows = [
            _reservation(1, self.coaches[0], self.start, 10),
            _reservation(2, self.coaches[0], self.start, 11),
            _reservation(3, self.coaches[1], self.start, 12),
        ]
        execution.return_value = (
            rows,
            {
                "availability:10": {"status": "held"},
                "availability:11": {"status": "scheduled"},
                "availability:12": {"status": "held"},
            },
        )
        saved_filter.return_value = self._saved_rows(
            {1: {"ball_expense_burden": 67}, 2: {"ball_expense_burden": 34}}
        )
        result = diagnose_closed_settlement(self.settlement, self.coaches)
        self.assertTrue(result["is_candidate"])
        self.assertEqual(result["scheduled_count"], 1)
        self.assertEqual(result["coach_rows"][0]["old_count"], 2)
        self.assertEqual(result["coach_rows"][0]["current_count"], 1)
        self.assertEqual(result["coach_rows"][0]["reference_burden"], 50)
        self.assertEqual(result["coach_rows"][0]["difference"], -17)

    @patch("club.settlement_integrity_diagnostic.CoachMonthlySettlement.objects.filter")
    @patch("club.settlement_integrity_diagnostic._ball_amount", return_value=0)
    @patch("club.settlement_integrity_diagnostic._monthly_execution_reservations_and_status")
    def test_scheduled_without_ball_expense_is_not_false_candidate(
        self, execution, _amount, saved_filter
    ):
        execution.return_value = (
            [_reservation(1, self.coaches[0], self.start, 10)],
            {"availability:10": {"status": "scheduled"}},
        )
        saved_filter.return_value = self._saved_rows({})
        result = diagnose_closed_settlement(self.settlement, self.coaches)
        self.assertFalse(result["is_candidate"])
        self.assertEqual(result["ball_amount"], 0)

    @patch("club.settlement_integrity_diagnostic.CoachMonthlySettlement.objects.filter")
    @patch("club.settlement_integrity_diagnostic._ball_amount", return_value=100)
    @patch("club.settlement_integrity_diagnostic._monthly_execution_reservations_and_status")
    def test_no_scheduled_means_no_effect(
        self, execution, _amount, saved_filter
    ):
        execution.return_value = (
            [_reservation(1, self.coaches[0], self.start, 10)],
            {"availability:10": {"status": "held"}},
        )
        saved_filter.return_value = self._saved_rows({})
        self.assertFalse(
            diagnose_closed_settlement(self.settlement, self.coaches)["is_candidate"]
        )

    @patch("club.settlement_integrity_diagnostic.diagnose_closed_settlement")
    @patch("club.settlement_integrity_diagnostic.main_coaches", return_value=[])
    @patch("club.settlement_integrity_diagnostic.MonthlySettlement.objects.filter")
    def test_multiple_month_list_uses_closed_only_and_filters_no_effect(
        self, settlement_filter, _coaches, diagnose
    ):
        queryset = MagicMock()
        queryset.order_by.return_value = [
            SimpleNamespace(year=2026, month=1),
            SimpleNamespace(year=2025, month=12),
        ]
        settlement_filter.return_value = queryset
        diagnose.side_effect = [
            {"month_label": "2026年1月", "is_candidate": True},
            {"month_label": "2025年12月", "is_candidate": False},
        ]
        self.assertEqual(
            affected_closed_settlements(),
            [{"month_label": "2026年1月", "is_candidate": True}],
        )
        settlement_filter.assert_called_once_with(status="closed")


class DiagnosticAccessTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user(
            "diagnostic_staff", password="password12345", is_staff=True
        )
        self.superuser = User.objects.create_superuser(
            "diagnostic_super", "super@example.com", "password12345"
        )
        self.coach = User.objects.create_user(
            "diagnostic_coach", password="password12345", role=User.ROLE_COACH
        )
        self.member = User.objects.create_user(
            "diagnostic_member", password="password12345", role=User.ROLE_MEMBER
        )
        self.url = reverse("club:settlement_integrity_diagnostic")

    @patch("club.settlement_integrity_views.affected_closed_settlements", return_value=[])
    def test_staff_and_superuser_can_view_with_get_only(self, diagnostic):
        for user in (self.staff, self.superuser):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(diagnostic.call_count, 2)
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_coach_member_and_anonymous_are_rejected(self):
        for user in (self.coach, self.member):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("club:login"), response.url)

    @patch("club.settlement_integrity_views.affected_closed_settlements", return_value=[])
    def test_repeated_get_does_not_create_or_change_settlement(self, _diagnostic):
        settlement = MonthlySettlement.objects.create(
            year=2025,
            month=12,
            status=MonthlySettlement.STATUS_CLOSED,
            calculation_snapshot={"sentinel": "unchanged"},
        )
        coach_row = CoachMonthlySettlement.objects.create(
            monthly_settlement=settlement,
            coach=self.staff,
            calculation_snapshot={"negative_carry": 321},
        )
        before = MonthlySettlement.objects.count()
        payment_count = SettlementPayment.objects.count()
        self.client.force_login(self.staff)
        self.client.get(self.url)
        self.client.get(self.url)
        settlement.refresh_from_db()
        self.assertEqual(MonthlySettlement.objects.count(), before)
        self.assertEqual(SettlementPayment.objects.count(), payment_count)
        self.assertEqual(settlement.calculation_snapshot, {"sentinel": "unchanged"})
        coach_row.refresh_from_db()
        self.assertEqual(coach_row.calculation_snapshot, {"negative_carry": 321})
