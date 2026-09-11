import copy
from datetime import timedelta

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from club.admin import CoachAvailabilityAdminForm, CourtAdmin, StringingOrderAdminForm
from club.court_service import save_court_update
from club.expense_admin_type_editor import EXPENSE_TYPE_COMMON, EXPENSE_TYPE_PERSONAL, _serialize_note
from club.expense_service import save_expense_update, update_ball_expense_application_month
from club.models import CoachAvailability, CoachExpense, Court, StringingOrder
from club.settlement_models import MonthlySettlement
from club.settlement_service import calculate_monthly_settlement
from club.stringing_service import create_stringing_order, save_admin_stringing_order


class AccountingAdminSafetyTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.admin = users.objects.create_superuser("safety-admin", "a@example.com", "pw")
        self.member = users.objects.create_user("safety-member")
        self.iizuka = users.objects.create_user("safety-iizuka", full_name="飯塚研太朗", role=users.ROLE_COACH)
        self.shimizu = users.objects.create_user("safety-shimizu", full_name="清水峻平", role=users.ROLE_COACH)
        self.inoue = users.objects.create_user("safety-inoue", full_name="井上春佳", role=users.ROLE_COACH)

    def close_month(self, value):
        settlement, _ = MonthlySettlement.objects.update_or_create(
            year=value.year, month=value.month,
            defaults={"status": MonthlySettlement.STATUS_CLOSED,
                      "calculation_snapshot": {"sentinel": "unchanged"}},
        )
        return settlement

    def test_open_expense_amount_update_recalculates_settlement(self):
        today = timezone.localdate()
        expense = CoachExpense.objects.create(
            expense_date=today, amount=100, created_by=self.iizuka,
            note=_serialize_note({"expense_type": EXPENSE_TYPE_COMMON}, "test"),
        )
        candidate = copy.copy(expense)
        candidate.amount = 250
        save_expense_update(candidate=candidate)
        calculate_monthly_settlement(today.year, today.month, force=True)
        self.assertEqual(CoachExpense.objects.get(pk=expense.pk).amount, 250)
        self.assertTrue(MonthlySettlement.objects.filter(year=today.year, month=today.month).exists())

    def test_closed_expense_amount_and_type_updates_are_rejected_and_snapshot_stays(self):
        today = timezone.localdate()
        expense = CoachExpense.objects.create(
            expense_date=today, amount=100,
            note=_serialize_note({"expense_type": EXPENSE_TYPE_COMMON}, "test"),
        )
        settlement = self.close_month(today)
        for change in ("amount", "type"):
            candidate = copy.copy(expense)
            if change == "amount":
                candidate.amount = 999
            else:
                candidate.note = _serialize_note({"expense_type": EXPENSE_TYPE_PERSONAL}, "test")
            with self.assertRaises(ValidationError):
                save_expense_update(candidate=candidate)
        settlement.refresh_from_db()
        self.assertEqual(settlement.calculation_snapshot, {"sentinel": "unchanged"})

    def test_ball_application_month_open_to_open_and_closed_side_rejected(self):
        month = timezone.localdate().replace(day=1)
        next_month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        expense = CoachExpense.objects.create(
            expense_date=month, category=CoachExpense.CATEGORY_BALL, amount=100,
            settlement_period_start=month, settlement_period_end=month,
        )
        update_ball_expense_application_month(expense=expense, application_month=next_month, user=self.admin)
        expense.refresh_from_db()
        self.assertEqual(expense.settlement_period_start, next_month)
        self.close_month(next_month)
        with self.assertRaises(ValidationError):
            update_ball_expense_application_month(expense=expense, application_month=month, user=self.admin)

    def _order(self, coach):
        return create_stringing_order(
            order=StringingOrder(assigned_coach=coach, preferred_delivery_time="来週"),
            user=self.member,
        )

    def test_stringing_admin_eligible_coaches_and_delivery_price(self):
        for coach in (self.iizuka, self.shimizu):
            self.assertEqual(self._order(coach).assigned_coach, coach)
        order = self._order(self.iizuka)
        candidate = copy.copy(order)
        candidate.delivery_requested = True
        candidate.delivery_location = "コート"
        candidate.preferred_delivery_time = "明日"
        candidate.delivery_fee = 99999
        save_admin_stringing_order(candidate=candidate)
        candidate.refresh_from_db()
        self.assertEqual(candidate.total_price(), 1700)
        invalid = copy.copy(candidate)
        invalid.assigned_coach = self.inoue
        with self.assertRaises(ValidationError):
            save_admin_stringing_order(candidate=invalid)

    def test_closed_stringing_change_is_rejected_and_admin_form_rejects_tampered_coach(self):
        order = self._order(self.iizuka)
        self.close_month(timezone.localtime(order.created_at).date())
        candidate = copy.copy(order)
        candidate.status = StringingOrder.STATUS_CANCELED
        with self.assertRaises(ValidationError):
            save_admin_stringing_order(candidate=candidate)
        form = StringingOrderAdminForm(data={
            "user": self.member.pk, "assigned_coach": self.inoue.pk,
            "status": order.status, "racket_name": "", "string_name": "",
            "tension_lbs": 50, "delivery_requested": False,
            "delivery_location": "", "preferred_delivery_time": "来週", "note": "",
        }, instance=order)
        self.assertFalse(form.is_valid())

    def test_court_capacity_decrease_and_inactive_history_contract(self):
        court = Court.objects.create(name="安全コート", available_court_count=3)
        start = (timezone.now() + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
        for index in range(2):
            CoachAvailability.objects.create(
                coach=(self.iizuka, self.shimizu)[index], court=court,
                start_at=start, end_at=start + timedelta(hours=2), court_count=1,
            )
        allowed = copy.copy(court)
        allowed.available_court_count = 2
        save_court_update(candidate=allowed)
        rejected = copy.copy(allowed)
        rejected.available_court_count = 1
        with self.assertRaises(ValidationError):
            save_court_update(candidate=rejected)
        allowed.is_active = False
        save_court_update(candidate=allowed)
        self.assertEqual(CoachAvailability.objects.filter(court=allowed).count(), 2)
        form = CoachAvailabilityAdminForm()
        self.assertNotIn(allowed, form.fields["court"].queryset)
        self.assertEqual(CourtAdmin(Court, AdminSite()).actions, ())
