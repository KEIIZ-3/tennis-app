from datetime import date, datetime, time, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.expense_metadata import build_expense_note, parse_expense_note
from club.lesson_execution_storage import save_status
from club.models import (
    MAIN_COACH_NAMES,
    CoachAvailability,
    CoachExpense,
    Court,
    FixedLesson,
    RainRefund,
    Reservation,
    User,
)
from club.settlement_models import MonthlySettlement


class LessonMemberListCourtAccountingTests(TestCase):
    def setUp(self):
        self.coaches = [
            User.objects.create_user(
                username=f"court-info-coach-{index}",
                role=User.ROLE_COACH,
                full_name=name,
            )
            for index, name in enumerate(MAIN_COACH_NAMES)
        ]
        self.court = Court.objects.create(name="西猪名公園", is_active=True)
        self.member = User.objects.create_user(
            username="court-info-member-fixture",
            role=User.ROLE_MEMBER,
        )
        self.start = timezone.make_aware(datetime.combine(date(2026, 7, 10), time(10)))
        self.availability = CoachAvailability.objects.create(
            coach=self.coaches[0],
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            start_at=self.start,
            end_at=self.start + timedelta(hours=2),
            capacity=4,
        )
        self.url = reverse("club:lesson_calendar_member_list")
        self.client.force_login(self.coaches[0])

    def _get(self, **params):
        values = {"availability_id": self.availability.pk}
        values.update(params)
        return self.client.get(self.url, values)

    def _post(self, **data):
        values = {
            "action": "update_court_accounting",
            "court_booking_account_choice": "",
            "court_booking_account_other": "",
            "court_payer_choice": "",
        }
        values.update(data)
        return self.client.post(f"{self.url}?availability_id={self.availability.pk}", values)

    def test_section_displays_unset_coach_company_and_other_values(self):
        response = self._get()
        self.assertContains(response, 'data-testid="court-accounting-section"')
        self.assertContains(response, "西猪名公園")
        self.assertContains(response, "未設定", count=None)

        self.availability.court_payer_kind = "company_wallet"
        self.availability.court_booking_account_kind = "coach"
        self.availability.court_booking_account_coach = self.coaches[1]
        self.availability.save(update_fields=[
            "court_payer_kind", "court_booking_account_kind",
            "court_booking_account_coach",
        ])
        response = self._get()
        self.assertContains(response, "会社の財布")
        self.assertContains(response, self.coaches[1].display_name())

        self.availability.court_payer_kind = "coach"
        self.availability.court_payer_coach = self.coaches[2]
        self.availability.save(update_fields=[
            "court_payer_kind", "court_payer_coach",
        ])
        self.assertContains(self._get(), self.coaches[2].display_name())

        self.availability.court_booking_account_kind = "other"
        self.availability.court_booking_account_coach = None
        self.availability.court_booking_account_other = "公園共通アカウント"
        self.availability.save(update_fields=[
            "court_booking_account_kind", "court_booking_account_coach",
            "court_booking_account_other",
        ])
        self.assertContains(self._get(), "公園共通アカウント")

    def test_same_page_saves_and_can_return_both_values_to_unset(self):
        settlement_count = MonthlySettlement.objects.count()
        expense_count = CoachExpense.objects.count()
        response = self._post(
            court_booking_account_choice=f"coach:{self.coaches[1].pk}",
            court_payer_choice="company_wallet",
        )
        self.assertRedirects(
            response, f"{self.url}?availability_id={self.availability.pk}",
            fetch_redirect_response=False,
        )
        self.availability.refresh_from_db()
        self.assertEqual(self.availability.court_payer_kind, "company_wallet")
        self.assertEqual(self.availability.court_booking_account_coach, self.coaches[1])
        self.assertEqual(MonthlySettlement.objects.count(), settlement_count)
        self.assertEqual(CoachExpense.objects.count(), expense_count)

        self._post()
        self.availability.refresh_from_db()
        self.assertEqual(self.availability.court_payer_kind, "")
        self.assertIsNone(self.availability.court_payer_coach)
        self.assertEqual(self.availability.court_booking_account_kind, "")
        self.assertIsNone(self.availability.court_booking_account_coach)

    def test_automatic_fee_is_displayed_and_manual_zero_is_preserved(self):
        response = self._get()
        self.assertContains(response, "1,800円")
        self.assertContains(response, "自動計算：1,800円")

        self._post(court_fee_amount="0")
        self.availability.refresh_from_db()
        self.assertEqual(self.availability.court_fee_amount, 0)
        self.assertTrue(self.availability.court_fee_overridden)
        self.assertContains(self._get(), "0円")
        self.assertContains(self._get(), "実績")

    def test_manual_fee_saves_as_canonical_paid_transfer(self):
        self._post(
            court_fee_amount="2200",
            court_booking_account_choice=f"coach:{self.coaches[1].pk}",
            court_payer_choice="company_wallet",
        )
        self.availability.refresh_from_db()
        self.assertEqual(self.availability.court_fee_amount, 2200)
        self.assertTrue(self.availability.court_fee_overridden)
        self.assertEqual(self.availability.court_booking_account_coach, self.coaches[1])
        self.assertEqual(self.availability.court_payer_kind, "company_wallet")
        expense = CoachExpense.objects.get()
        meta = parse_expense_note(expense.note)
        self.assertEqual(expense.amount, 2200)
        self.assertEqual(meta["record_kind"], "court_transfer")
        self.assertEqual(meta["availability_id"], self.availability.pk)
        self.assertEqual(meta["payer_kind"], "company_wallet")
        settlement = MonthlySettlement.objects.get()
        self.assertEqual(
            settlement.calculation_snapshot["company_paid_court_total"], 2200
        )
        self.assertEqual(settlement.cash_out_total, 2200)
        self.assertEqual(settlement.closing_balance, -2200)

        response = self._get()
        self.assertContains(response, "登録済み")
        self.assertContains(response, "2,200円")

    def test_resave_updates_one_transfer_and_latest_payer_only(self):
        self._post(court_fee_amount="3200", court_payer_choice="company_wallet")
        expense = CoachExpense.objects.get()
        self._post(
            court_fee_amount="3000",
            court_payer_choice=f"coach:{self.coaches[1].pk}",
        )
        self.assertEqual(CoachExpense.objects.count(), 1)
        expense.refresh_from_db()
        meta = parse_expense_note(expense.note)
        self.assertEqual(expense.amount, 3000)
        self.assertEqual(meta["payer_kind"], "coach")
        self.assertEqual(meta["payer_coach_id"], self.coaches[1].pk)
        settlement = MonthlySettlement.objects.get()
        self.assertEqual(
            settlement.calculation_snapshot["company_paid_court_total"], 0
        )
        self.coaches[0].is_staff = True
        self.coaches[0].save(update_fields=["is_staff"])
        response = self.client.get(reverse("club:coach_admin_settlement"), {
            "year": self.start.year,
            "month": self.start.month,
        })
        self.assertContains(response, "会社支払コート代")
        self.assertContains(
            response,
            '<div class="summary-value">0円</div><div class="summary-note">'
            "会社財布から直接支払った当月のコート代</div>",
            html=True,
        )

    def test_rain_canceled_overlap_allows_fee_update_and_reuses_transfer(self):
        canceled = CoachAvailability.objects.create(
            coach=self.coaches[0],
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            start_at=self.start + timedelta(days=1),
            end_at=self.start + timedelta(days=1, hours=2),
            capacity=4,
        )
        CoachAvailability.objects.filter(pk=canceled.pk).update(
            start_at=self.start,
            end_at=self.start + timedelta(hours=2),
        )
        canceled.refresh_from_db()
        Reservation.objects.create(
            user=self.member,
            coach=self.coaches[0],
            court=self.court,
            availability=canceled,
            lesson_type=Reservation.LESSON_GENERAL,
            start_at=self.start,
            end_at=self.start + timedelta(hours=2),
            status=Reservation.STATUS_RAIN_CANCELED,
            cancellation_reason="雨天中止",
        )
        settlement, _created = MonthlySettlement.objects.get_or_create(
            year=self.start.year,
            month=self.start.month,
        )
        save_status(
            settlement,
            f"availability:{canceled.pk}",
            "refund_pending",
            self.coaches[0],
            cancellation_type="rain",
        )
        refund_expense = CoachExpense.objects.create(
            expense_date=self.start.date(),
            category=CoachExpense.CATEGORY_COURT,
            amount=1800,
            created_by=self.coaches[0],
        )
        RainRefund.objects.create(
            expense=refund_expense,
            availability=canceled,
            lesson_date=self.start.date(),
            amount=1800,
            status=RainRefund.STATUS_VOIDED,
            booking_account_kind=RainRefund.ACCOUNT_COACH,
            booking_account_coach=self.coaches[0],
            debit_coach=self.coaches[0],
        )

        self._post(court_fee_amount="2400", court_payer_choice="company_wallet")
        self._post(
            court_fee_amount="2600",
            court_payer_choice=f"coach:{self.coaches[1].pk}",
        )

        self.availability.refresh_from_db()
        transfers = [
            expense
            for expense in CoachExpense.objects.all()
            if parse_expense_note(expense.note).get("record_kind") == "court_transfer"
            and parse_expense_note(expense.note).get("availability_id") == self.availability.pk
        ]
        self.assertEqual(self.availability.court_fee_amount, 2600)
        self.assertEqual(self.availability.court_payer_coach, self.coaches[1])
        self.assertEqual(len(transfers), 1)
        self.assertEqual(transfers[0].amount, 2600)

    def test_zero_is_registered_without_wallet_cash_out(self):
        self._post(court_fee_amount="0", court_payer_choice="company_wallet")
        expense = CoachExpense.objects.get()
        meta = parse_expense_note(expense.note)
        self.assertEqual(expense.amount, 0)
        self.assertTrue(meta["court_cost_not_required"])
        settlement = MonthlySettlement.objects.get()
        self.assertEqual(
            settlement.calculation_snapshot["company_paid_court_total"], 0
        )

    def test_unknown_court_accepts_manual_fee(self):
        self.court.name = "その他"
        self.court.court_type = Court.COURT_OTHER
        self.court.save(update_fields=["name", "court_type"])
        self._post(court_fee_amount="900", court_payer_choice="company_wallet")
        self.availability.refresh_from_db()
        self.assertEqual(self.availability.court_fee_amount, 900)
        self.assertTrue(self.availability.court_fee_overridden)
        self.assertEqual(CoachExpense.objects.get().amount, 900)

    def test_other_requires_name_and_invalid_coach_is_rejected(self):
        response = self._post(court_booking_account_choice="other")
        self.assertEqual(response.status_code, 302)
        self.availability.refresh_from_db()
        self.assertEqual(self.availability.court_booking_account_kind, "")

        response = self._post(court_payer_choice="coach:999999")
        self.assertEqual(response.status_code, 302)
        self.availability.refresh_from_db()
        self.assertEqual(self.availability.court_payer_kind, "")

    def test_actual_court_transfer_payer_is_displayed_and_updated_canonically(self):
        CoachExpense.objects.create(
            expense_date=self.start.date(),
            category=CoachExpense.CATEGORY_COURT,
            amount=2400,
            created_by=self.coaches[0],
            note=build_expense_note({
                "record_kind": "court_transfer",
                "availability_id": self.availability.pk,
                "approval_status": "approved",
                "payer_kind": "coach",
                "payer_coach_id": self.coaches[1].pk,
                "payer_coach_name": self.coaches[1].display_name(),
            }),
        )
        response = self._get()
        self.assertContains(response, "実績支払元")
        self.assertContains(response, self.coaches[1].display_name())

        self._post(court_fee_amount="2400", court_payer_choice="company_wallet")
        self.availability.refresh_from_db()
        self.assertEqual(self.availability.court_payer_kind, "company_wallet")
        self.assertEqual(CoachExpense.objects.count(), 1)
        self.assertEqual(
            parse_expense_note(CoachExpense.objects.get().note)["payer_kind"],
            "company_wallet",
        )

    def test_member_cannot_see_or_post_edit_ui(self):
        member = User.objects.create_user(username="court-info-member", role=User.ROLE_MEMBER)
        self.client.force_login(member)
        response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'data-testid="court-accounting-section"')
        response = self._post(court_payer_choice="company_wallet")
        self.assertEqual(response.status_code, 403)

    def test_unassigned_contractor_cannot_change_values(self):
        contractor = User.objects.create_user(
            username="other-contractor", role=User.ROLE_CONTRACTOR_COACH
        )
        self.client.force_login(contractor)
        response = self._post(court_payer_choice="company_wallet")
        self.assertEqual(response.status_code, 403)
        self.availability.refresh_from_db()
        self.assertEqual(self.availability.court_payer_kind, "")

    def test_fixed_occurrence_is_materialized_without_affecting_next_week(self):
        fixed = FixedLesson.objects.create(
            title="固定一般",
            coach=self.coaches[0],
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_date=self.start.date(),
            weekday=self.start.date().weekday(),
            start_hour=10,
            capacity=4,
            coach_count=1,
            court_count=1,
            weeks_ahead=2,
        )
        self.availability.delete()
        fixed_url = (
            f"{self.url}?fixed_lesson_id={fixed.pk}"
            f"&lesson_date={self.start.date().isoformat()}"
        )
        response = self.client.post(fixed_url, {
            "action": "update_court_accounting",
            "court_fee_amount": "2200",
            "court_booking_account_choice": "other",
            "court_booking_account_other": "当日専用",
            "court_payer_choice": "company_wallet",
        })
        self.assertEqual(response.status_code, 302)
        occurrence = CoachAvailability.objects.get(fixed_lesson_source=fixed)
        self.assertEqual(occurrence.start_at, self.start)
        self.assertEqual(occurrence.court_booking_account_other, "当日専用")
        self.assertEqual(occurrence.court_payer_kind, "company_wallet")
        self.assertEqual(occurrence.court_fee_amount, 2200)
        self.assertFalse(
            CoachAvailability.objects.filter(
                fixed_lesson_source=fixed,
                start_at=self.start + timedelta(days=7),
            ).exists()
        )

    def test_closed_month_rejects_change(self):
        MonthlySettlement.objects.create(
            year=self.start.year,
            month=self.start.month,
            status=MonthlySettlement.STATUS_CLOSED,
        )
        response = self._post(court_payer_choice="company_wallet")
        self.assertEqual(response.status_code, 302)
        self.availability.refresh_from_db()
        self.assertEqual(self.availability.court_payer_kind, "")
