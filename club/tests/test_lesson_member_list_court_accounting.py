from datetime import date, datetime, time, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.expense_metadata import build_expense_note
from club.models import (
    MAIN_COACH_NAMES,
    CoachAvailability,
    CoachExpense,
    Court,
    FixedLesson,
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

    def test_other_requires_name_and_invalid_coach_is_rejected(self):
        response = self._post(court_booking_account_choice="other")
        self.assertEqual(response.status_code, 302)
        self.availability.refresh_from_db()
        self.assertEqual(self.availability.court_booking_account_kind, "")

        response = self._post(court_payer_choice="coach:999999")
        self.assertEqual(response.status_code, 302)
        self.availability.refresh_from_db()
        self.assertEqual(self.availability.court_payer_kind, "")

    def test_actual_court_transfer_payer_is_displayed_and_not_overwritten(self):
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

        self._post(court_payer_choice="company_wallet")
        self.availability.refresh_from_db()
        self.assertEqual(self.availability.court_payer_kind, "")

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
            "court_booking_account_choice": "other",
            "court_booking_account_other": "当日専用",
            "court_payer_choice": "company_wallet",
        })
        self.assertEqual(response.status_code, 302)
        occurrence = CoachAvailability.objects.get(fixed_lesson_source=fixed)
        self.assertEqual(occurrence.start_at, self.start)
        self.assertEqual(occurrence.court_booking_account_other, "当日専用")
        self.assertEqual(occurrence.court_payer_kind, "company_wallet")
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
