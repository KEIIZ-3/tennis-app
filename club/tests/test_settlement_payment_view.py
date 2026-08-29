from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from club.settlement_models import MonthlySettlement, SettlementPayment


class SettlementPaymentViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            username="settlement_payment_admin",
            password="password12345",
            is_staff=True,
        )
        self.coach = User.objects.create_user(
            username="settlement_payment_coach",
            password="password12345",
            role=User.ROLE_COACH,
        )
        self.settlement = MonthlySettlement.objects.create(
            year=2097,
            month=8,
            status=MonthlySettlement.STATUS_DRAFT,
        )
        self.url = reverse("club:coach_admin_settlement")
        self.payload = {
            "action": "create_payout",
            "year": "2097",
            "month": "8",
            "coach_id": str(self.coach.pk),
            "payout_type": "salary_payout",
            "amount": "1200",
            "paid_date": "2097-08-28",
            "note": "8月分",
        }

    def _post_with_service(self, service):
        self.client.force_login(self.admin)
        with (
            patch(
                "club.settlement_views.get_or_create_monthly_settlement",
                return_value=self.settlement,
            ),
            patch(
                "club.lesson_execution.unconfirmed_execution_rows",
                return_value=[],
            ),
            patch("club.settlement_views.create_settlement_payment", service),
        ):
            return self.client.post(self.url, self.payload, follow=True)

    def test_form_posts_fields_expected_by_create_payout_view(self):
        self.client.force_login(self.admin)
        with patch(
            "club.settlement_admin_refresh.calculate_monthly_settlement"
        ):
            response = self.client.get(
                self.url, {"year": self.settlement.year, "month": self.settlement.month}
            )

        self.assertContains(response, 'name="action" value="create_payout"')
        self.assertContains(response, 'name="payout_type" value="salary_payout"')
        for field_name in ("year", "month", "coach_id", "amount", "paid_date", "note"):
            self.assertContains(response, f'name="{field_name}"')
        self.assertContains(response, "csrfmiddlewaretoken")
        self.assertContains(response, "支払いを記録")

    def test_admin_create_payout_passes_normalized_fields_to_service(self):
        payment = SimpleNamespace(pk=1)
        with patch(
            "club.settlement_views.create_settlement_payment",
            return_value=(payment, True, 0),
        ) as service:
            response = self._post_with_service(service)

        self.assertEqual(response.status_code, 200)
        service.assert_called_once_with(
            settlement=self.settlement,
            coach=self.coach,
            payment_type=SettlementPayment.PAYMENT_TYPE_SALARY,
            amount=1200,
            paid_date=date(2097, 8, 28),
            note="8月分",
            user=self.admin,
        )
        self.assertContains(response, "給与 1,200円を記録しました。")

    def test_admin_create_payout_creates_settlement_payment(self):
        self.client.force_login(self.admin)
        with (
            patch(
                "club.settlement_views.get_or_create_monthly_settlement",
                return_value=self.settlement,
            ),
            patch(
                "club.lesson_execution.unconfirmed_execution_rows",
                return_value=[],
            ),
            patch.object(SettlementPayment, "_validate_wallet_payment"),
            patch(
                "club.settlement_service.calculate_monthly_settlement",
                return_value={},
            ),
        ):
            response = self.client.post(self.url, self.payload)

        self.assertEqual(response.status_code, 302)
        payment = SettlementPayment.objects.get()
        self.assertEqual(payment.monthly_settlement, self.settlement)
        self.assertEqual(payment.coach, self.coach)
        self.assertEqual(payment.payment_type, SettlementPayment.PAYMENT_TYPE_SALARY)
        self.assertEqual(payment.amount, 1200)
        self.assertEqual(payment.paid_date, date(2097, 8, 28))
        self.assertEqual(payment.note, "8月分")
        self.assertEqual(payment.created_by, self.admin)

    def test_non_admin_cannot_create_payout(self):
        self.client.force_login(self.coach)
        response = self.client.post(self.url, self.payload)

        self.assertEqual(response.status_code, 403)
        self.assertFalse(SettlementPayment.objects.exists())

    def test_missing_coach_and_non_positive_amount_are_rejected(self):
        self.client.force_login(self.admin)
        with patch(
            "club.settlement_views.get_or_create_monthly_settlement",
            return_value=self.settlement,
        ):
            missing_coach = self.payload | {"coach_id": ""}
            response = self.client.post(self.url, missing_coach, follow=True)
            self.assertContains(response, "支払先コーチを選択してください。")

            for amount in ("0", "-1"):
                response = self.client.post(
                    self.url, self.payload | {"amount": amount}, follow=True
                )
                self.assertContains(response, "金額は1円以上で入力してください。")

        self.assertFalse(SettlementPayment.objects.exists())

    def test_validation_error_is_displayed_without_server_error(self):
        service = patch(
            "club.settlement_views.create_settlement_payment",
            side_effect=ValidationError(
                "支払額がこのコーチの支払可能額を超えています。支払可能上限は1,000円です。"
            ),
        ).start()
        self.addCleanup(patch.stopall)

        response = self._post_with_service(service)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "支払額がこのコーチの支払可能額を超えています。")
        self.assertFalse(SettlementPayment.objects.exists())

    def test_closed_month_is_rejected_before_service_call(self):
        self.settlement.status = MonthlySettlement.STATUS_CLOSED
        self.settlement.save(update_fields=["status"])
        service = patch("club.settlement_views.create_settlement_payment").start()
        self.addCleanup(patch.stopall)

        response = self._post_with_service(service)

        self.assertContains(response, "締め済みの月には支払いを追加できません。")
        service.assert_not_called()

    def test_duplicate_result_reports_idempotent_outcome(self):
        service = patch(
            "club.settlement_views.create_settlement_payment",
            return_value=(SimpleNamespace(pk=1), False, 0),
        ).start()
        self.addCleanup(patch.stopall)

        response = self._post_with_service(service)

        self.assertContains(response, "重複登録しませんでした。")
        service.assert_called_once()
