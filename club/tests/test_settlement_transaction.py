from datetime import date, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from club.settlement_models import (
    CoachMonthlySettlement,
    MonthlySettlement,
    SettlementPayment,
)
from club.settlement_service import calculate_monthly_settlement
from club.tests.test_settlement_closed_recalculation import (
    COACH_FIELDS,
    MONTHLY_FIELDS,
    PAYMENT_FIELDS,
)


class MonthlySettlementTransactionTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.coach = User.objects.create_user(
            username="settlement_transaction_coach",
            password="password12345",
            role=User.ROLE_COACH,
        )

    def _saved_state(self, settlement):
        return {
            "settlement": list(
                MonthlySettlement.objects.filter(pk=settlement.pk).values(
                    *MONTHLY_FIELDS
                )
            ),
            "coach_rows": list(
                CoachMonthlySettlement.objects.filter(
                    monthly_settlement=settlement
                )
                .order_by("pk")
                .values(*COACH_FIELDS)
            ),
            "payments": list(
                SettlementPayment.objects.filter(
                    monthly_settlement=settlement
                )
                .order_by("pk")
                .values(*PAYMENT_FIELDS)
            ),
        }

    def test_wallet_policy_failure_rolls_back_all_existing_month_writes(self):
        fixed_time = timezone.make_aware(datetime(2098, 7, 1, 9, 0))
        settlement = MonthlySettlement.objects.create(
            year=2098,
            month=7,
            opening_balance=111,
            cash_in_total=222,
            cash_out_total=33,
            closing_balance=300,
            calculation_snapshot={"before": "monthly"},
            created_at=fixed_time,
            updated_at=fixed_time,
        )
        CoachMonthlySettlement.objects.create(
            monthly_settlement=settlement,
            coach=self.coach,
            lesson_count=4,
            salary_due=500,
            salary_unpaid=500,
            calculation_snapshot={"before": "coach"},
            created_at=fixed_time,
            updated_at=fixed_time,
        )
        SettlementPayment.objects.bulk_create(
            [
                SettlementPayment(
                    monthly_settlement=settlement,
                    coach=self.coach,
                    payment_type=SettlementPayment.PAYMENT_TYPE_SALARY,
                    amount=50,
                    paid_date=date(2098, 7, 1),
                    note="before payment",
                    created_at=fixed_time,
                    updated_at=fixed_time,
                )
            ]
        )
        before = self._saved_state(settlement)

        def legacy_sync_write(_end_date):
            SettlementPayment.objects.bulk_create(
                [
                    SettlementPayment(
                        monthly_settlement=settlement,
                        coach=self.coach,
                        payment_type=SettlementPayment.PAYMENT_TYPE_SALARY,
                        amount=75,
                        paid_date=date(2098, 7, 2),
                        note="legacy sync write",
                        legacy_coach_expense_id=987654,
                    )
                ]
            )

        with (
            patch(
                "club.settlement_service.sync_legacy_payouts_through",
                side_effect=legacy_sync_write,
            ),
            patch(
                "club.settlement_balance_policy._apply_wallet_policy",
                side_effect=RuntimeError("wallet policy failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "wallet policy failed"):
                calculate_monthly_settlement(2098, 7, force=True)

        self.assertEqual(self._saved_state(settlement), before)

    def test_wallet_policy_failure_leaves_no_incomplete_new_month_rows(self):
        with patch(
            "club.settlement_balance_policy._apply_wallet_policy",
            side_effect=RuntimeError("wallet policy failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "wallet policy failed"):
                calculate_monthly_settlement(2098, 8)

        self.assertFalse(
            MonthlySettlement.objects.filter(year=2098, month=8).exists()
        )
        self.assertFalse(
            CoachMonthlySettlement.objects.filter(
                monthly_settlement__year=2098,
                monthly_settlement__month=8,
            ).exists()
        )
        self.assertFalse(
            SettlementPayment.objects.filter(
                monthly_settlement__year=2098,
                monthly_settlement__month=8,
            ).exists()
        )
