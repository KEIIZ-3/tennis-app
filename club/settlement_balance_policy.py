from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.utils import timezone


MAIN_COACH_NAMES = (
    "飯塚研太朗",
    "清水峻平",
    "井上春佳",
)


def _money(value):
    try:
        return int(value or 0)
    except Exception:
        return 0


def _display_name(user):
    if not user:
        return ""
    try:
        return str(user.display_name() or "").strip()
    except Exception:
        return str(
            getattr(user, "full_name", "")
            or getattr(user, "username", "")
            or ""
        ).strip()


def _previous_year_month(year, month):
    year = int(year)
    month = int(month)
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _main_coach_ids():
    from django.contrib.auth import get_user_model

    User = get_user_model()
    result = {}

    for user in User.objects.filter(
        role="coach",
    ).order_by("id"):
        name = _display_name(user)
        if name in MAIN_COACH_NAMES:
            result[name] = user.pk

    return {
        name: result[name]
        for name in MAIN_COACH_NAMES
        if name in result
    }


def _previous_compensation_balance(coach, year, month):
    from .settlement_models import CoachMonthlySettlement

    previous_year, previous_month = _previous_year_month(year, month)

    previous_row = (
        CoachMonthlySettlement.objects.filter(
            monthly_settlement__year=previous_year,
            monthly_settlement__month=previous_month,
            coach=coach,
        )
        .select_related("monthly_settlement")
        .first()
    )
    if previous_row is None:
        return 0

    snapshot = previous_row.calculation_snapshot or {}
    if "closing_compensation_balance" in snapshot:
        return _money(snapshot.get("closing_compensation_balance"))

    # 新方式導入前のデータは、未払給与をプラス残高として引き継ぎます。
    return _money(previous_row.salary_unpaid)


def _active_salary_payment_total(settlement, coach):
    from .settlement_models import SettlementPayment

    result = SettlementPayment.objects.filter(
        monthly_settlement=settlement,
        coach=coach,
        payment_type=SettlementPayment.PAYMENT_TYPE_SALARY,
        is_reversed=False,
    ).aggregate(total=Sum("amount"))
    return _money(result.get("total"))


def _apply_three_coach_balance_policy(result, year, month):
    from .settlement_models import CoachMonthlySettlement

    settlement = result.get("settlement")
    if settlement is None or settlement.is_closed:
        return result

    coach_rows = list(result.get("coach_rows") or [])
    if not coach_rows:
        return result

    main_ids_by_name = _main_coach_ids()
    main_coach_ids = set(main_ids_by_name.values())

    approved_common_expense_total = _money(
        result.get("approved_common_expense_total")
    )
    contractor_hourly_pay_total = _money(
        result.get("contractor_hourly_pay_total")
    )

    shared_expense_total = (
        approved_common_expense_total
        + contractor_hourly_pay_total
    )

    # 端数は切り捨て。3人未登録でも分母は必ず3人固定です。
    per_main_coach_share = int(shared_expense_total / 3)

    unpaid_salary_total = 0
    negative_carry_total = 0
    salary_due_total = 0
    salary_paid_total = 0

    for row in coach_rows:
        coach = row.get("coach")
        coach_id = getattr(coach, "pk", None)
        is_contractor = bool(row.get("is_contractor_coach"))

        if coach_id in main_coach_ids:
            common_expense_share = per_main_coach_share
        else:
            common_expense_share = 0

        if is_contractor:
            current_month_compensation = (
                _money(row.get("contractor_hourly_pay_amount"))
                + _money(row.get("stringing_amount"))
            )
        else:
            current_month_compensation = (
                _money(row.get("ticket_amount"))
                + _money(row.get("preopen_paid_amount"))
                + _money(row.get("stringing_amount"))
                - common_expense_share
            )

        opening_compensation_balance = _previous_compensation_balance(
            coach,
            year,
            month,
        )
        salary_paid = _active_salary_payment_total(settlement, coach)

        balance_before_payment = (
            opening_compensation_balance
            + current_month_compensation
        )
        closing_compensation_balance = (
            balance_before_payment
            - salary_paid
        )

        salary_due = max(balance_before_payment, 0)
        unpaid_salary = max(closing_compensation_balance, 0)
        negative_carry = max(-closing_compensation_balance, 0)

        row.update(
            {
                "common_expense_share": common_expense_share,
                "current_month_compensation": current_month_compensation,
                "opening_compensation_balance": opening_compensation_balance,
                "balance_before_payment": balance_before_payment,
                "salary_due": salary_due,
                "salary_paid": salary_paid,
                "unpaid_salary": unpaid_salary,
                "negative_carry": negative_carry,
                "closing_compensation_balance": closing_compensation_balance,
                "total_unpaid": (
                    unpaid_salary
                    + _money(row.get("unpaid_reimbursement"))
                ),
                "total_paid": (
                    salary_paid
                    + _money(row.get("reimbursement_paid"))
                ),
                "is_main_coach": coach_id in main_coach_ids,
            }
        )

        saved_row = CoachMonthlySettlement.objects.filter(
            monthly_settlement=settlement,
            coach=coach,
        ).first()

        if saved_row is not None:
            snapshot = dict(saved_row.calculation_snapshot or {})
            snapshot.update(
                {
                    "is_main_coach": coach_id in main_coach_ids,
                    "main_coach_names": list(MAIN_COACH_NAMES),
                    "shared_expense_total": shared_expense_total,
                    "shared_expense_divisor": 3,
                    "common_expense_share": common_expense_share,
                    "current_month_compensation": (
                        current_month_compensation
                    ),
                    "opening_compensation_balance": (
                        opening_compensation_balance
                    ),
                    "balance_before_payment": balance_before_payment,
                    "closing_compensation_balance": (
                        closing_compensation_balance
                    ),
                    "negative_carry": negative_carry,
                }
            )

            saved_row.common_expense_share = common_expense_share
            saved_row.salary_due = salary_due
            saved_row.salary_paid = salary_paid
            saved_row.salary_unpaid = unpaid_salary
            saved_row.calculation_snapshot = snapshot
            saved_row.updated_at = timezone.now()
            saved_row.save(
                update_fields=[
                    "common_expense_share",
                    "salary_due",
                    "salary_paid",
                    "salary_unpaid",
                    "calculation_snapshot",
                    "updated_at",
                ]
            )

        unpaid_salary_total += unpaid_salary
        negative_carry_total += negative_carry
        salary_due_total += salary_due
        salary_paid_total += salary_paid

    settlement.unpaid_salary_total = unpaid_salary_total

    snapshot = dict(settlement.calculation_snapshot or {})
    snapshot.update(
        {
            "main_coach_names": list(MAIN_COACH_NAMES),
            "main_coach_ids": main_ids_by_name,
            "common_expense_participant_count": 3,
            "shared_expense_total": shared_expense_total,
            "approved_common_expense_total": (
                approved_common_expense_total
            ),
            "contractor_hourly_pay_total": (
                contractor_hourly_pay_total
            ),
            "per_coach_common_expense": per_main_coach_share,
            "negative_carry_total": negative_carry_total,
        }
    )
    settlement.calculation_snapshot = snapshot
    settlement.updated_at = timezone.now()
    settlement.save(
        update_fields=[
            "unpaid_salary_total",
            "calculation_snapshot",
            "updated_at",
        ]
    )

    result.update(
        {
            "coach_rows": coach_rows,
            "common_expense_participant_count": 3,
            "common_expense_base_total": shared_expense_total,
            "per_coach_common_expense": per_main_coach_share,
            "salary_due_total": salary_due_total,
            "salary_paid_total": salary_paid_total,
            "unpaid_salary_total": unpaid_salary_total,
            "negative_carry_total": negative_carry_total,
            "main_coach_names": MAIN_COACH_NAMES,
            "main_coach_ids": main_ids_by_name,
        }
    )
    return result


def _reimbursement_outstanding_amount(coach, paid_date):
    from .settlement_service import (
        approved_personal_expenses_for_coach,
        expense_unpaid_amount,
    )

    total = 0
    expenses = approved_personal_expenses_for_coach(
        coach,
        before_date=paid_date + timedelta(days=1),
    )
    for expense in expenses:
        total += expense_unpaid_amount(
            expense,
            through_date=paid_date,
        )
    return total


def _apply_payment_overpayment_guard():
    from .settlement_models import SettlementPayment

    if getattr(
        SettlementPayment.save,
        "_settlement_overpayment_guard_applied",
        False,
    ):
        return

    original_save = SettlementPayment.save

    def guarded_save(self, *args, **kwargs):
        is_new = bool(getattr(self._state, "adding", False))

        if (
            is_new
            and not self.legacy_coach_expense_id
            and self.payment_type
            == SettlementPayment.PAYMENT_TYPE_REIMBURSEMENT
        ):
            paid_date = self.paid_date or timezone.localdate()
            outstanding = _reimbursement_outstanding_amount(
                self.coach,
                paid_date,
            )
            amount = _money(self.amount)

            if outstanding <= 0:
                raise ValidationError(
                    "このコーチには精算可能な本人立替がありません。"
                )

            if amount > outstanding:
                raise ValidationError(
                    "立替精算額が未精算残高を超えています。"
                    f"精算可能上限は{outstanding:,}円です。"
                )

        return original_save(self, *args, **kwargs)

    guarded_save._settlement_overpayment_guard_applied = True
    guarded_save._original_save = original_save
    SettlementPayment.save = guarded_save


def apply_settlement_balance_policy():
    from . import settlement_service

    if getattr(
        settlement_service.calculate_monthly_settlement,
        "_three_coach_balance_policy_applied",
        False,
    ):
        _apply_payment_overpayment_guard()
        return

    original_calculate = (
        settlement_service.calculate_monthly_settlement
    )

    def calculate_with_policy(year, month, *, force=False):
        result = original_calculate(
            year,
            month,
            force=force,
        )
        return _apply_three_coach_balance_policy(
            result,
            year,
            month,
        )

    calculate_with_policy._three_coach_balance_policy_applied = True
    calculate_with_policy._original_calculate = original_calculate

    settlement_service.calculate_monthly_settlement = (
        calculate_with_policy
    )

    _apply_payment_overpayment_guard()
