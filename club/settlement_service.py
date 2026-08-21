from datetime import date, datetime, time, timedelta

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .expense_metadata import (
    EXPENSE_APPROVAL_APPROVED,
    EXPENSE_APPROVAL_SUBMITTED,
    EXPENSE_TYPE_COMMON,
    EXPENSE_TYPE_PERSONAL,
    EXPENSE_TYPE_REIMBURSEMENT_PAYOUT,
    EXPENSE_TYPE_SALARY_PAYOUT,
    expense_meta_row,
)
from .settlement_builder import build_coach_map
from .settlement_calculator import (
    aggregate_reservations,
    aggregate_stringing_orders,
    calculate_coach_rows,
    classify_expense_rows,
)
from .models import (
    CoachExpense,
    Reservation,
    PREOPEN_CASH_PRICE,
    is_preopen_cash_lesson_date,
)
from .settlement_models import (
    CoachMonthlySettlement,
    ExpenseSettlementAllocation,
    MonthlySettlement,
    SettlementPayment,
)
from .settlement_loader import load_monthly_settlement_data
from .settlement_persistence import persist_monthly_settlement
from .settlement_reimbursement import apply_reimbursement_amounts
from .settlement_result import MonthlySettlementResult
from .settlement_totals import calculate_settlement_totals


def money(value):
    try:
        return int(value or 0)
    except Exception:
        return 0


def display_name(user):
    if not user:
        return "-"
    try:
        return user.display_name()
    except Exception:
        return getattr(user, "full_name", "") or getattr(user, "username", "-") or "-"


def month_range(year, month):
    month_start = date(int(year), int(month), 1)
    if int(month) == 12:
        next_month = date(int(year) + 1, 1, 1)
    else:
        next_month = date(int(year), int(month) + 1, 1)
    return month_start, next_month


def aware_month_range(year, month):
    month_start, next_month = month_range(year, month)
    start_at = timezone.make_aware(datetime.combine(month_start, time.min))
    end_at = timezone.make_aware(datetime.combine(next_month, time.min))
    return month_start, next_month, start_at, end_at


def expense_allocated_total(expense, *, through_date=None):
    filters = {
        "expense": expense,
        "payment__is_reversed": False,
    }
    if through_date is not None:
        filters["payment__paid_date__lte"] = through_date
    result = ExpenseSettlementAllocation.objects.filter(**filters).aggregate(
        total=Sum("amount")
    )
    return money(result.get("total"))


def expense_unpaid_amount(expense, *, through_date=None):
    return max(
        money(expense.amount)
        - expense_allocated_total(expense, through_date=through_date),
        0,
    )


def approved_personal_expenses_for_coach(coach, *, before_date=None):
    queryset = (
        CoachExpense.objects.filter(created_by=coach)
        .select_related("created_by")
        .order_by("expense_date", "id")
    )
    if before_date is not None:
        queryset = queryset.filter(expense_date__lt=before_date)

    expenses = []
    for expense in queryset:
        row = expense_meta_row(expense)
        if row["is_payout"]:
            continue
        if row["expense_type"] != EXPENSE_TYPE_PERSONAL:
            continue
        if row["approval_status"] != EXPENSE_APPROVAL_APPROVED:
            continue
        expenses.append(expense)
    return expenses


def get_or_create_monthly_settlement(year, month):
    previous_year = int(year)
    previous_month = int(month) - 1
    if previous_month == 0:
        previous_month = 12
        previous_year -= 1

    previous = MonthlySettlement.objects.filter(
        year=previous_year,
        month=previous_month,
    ).first()
    opening_balance = money(previous.closing_balance) if previous else 0

    settlement, created = MonthlySettlement.objects.get_or_create(
        year=int(year),
        month=int(month),
        defaults={
            "opening_balance": opening_balance,
            "closing_balance": opening_balance,
        },
    )
    if created:
        return settlement

    if not settlement.is_closed and settlement.opening_balance != opening_balance:
        settlement.opening_balance = opening_balance
        settlement.updated_at = timezone.now()
        settlement.save(update_fields=["opening_balance", "updated_at"])
    return settlement


@transaction.atomic
def allocate_reimbursement_fifo(payment):
    if payment.payment_type != SettlementPayment.PAYMENT_TYPE_REIMBURSEMENT:
        return 0
    if payment.is_reversed:
        return 0

    ExpenseSettlementAllocation.objects.filter(payment=payment).delete()

    remaining = money(payment.amount)
    allocated = 0
    allocation_order = 1

    expenses = approved_personal_expenses_for_coach(
        payment.coach,
        before_date=payment.paid_date + timedelta(days=1),
    )

    for expense in expenses:
        if remaining <= 0:
            break

        already_allocated = expense_allocated_total(
            expense,
            through_date=payment.paid_date,
        )
        unpaid = max(money(expense.amount) - already_allocated, 0)
        if unpaid <= 0:
            continue

        amount = min(unpaid, remaining)
        ExpenseSettlementAllocation.objects.create(
            payment=payment,
            expense=expense,
            amount=amount,
            allocation_order=allocation_order,
        )
        remaining -= amount
        allocated += amount
        allocation_order += 1

    return allocated


@transaction.atomic
def sync_legacy_payouts_through(end_date):
    legacy_rows = (
        CoachExpense.objects.filter(expense_date__lt=end_date)
        .select_related("created_by")
        .order_by("expense_date", "id")
    )

    synced = 0
    for expense in legacy_rows:
        row = expense_meta_row(expense)
        if not row["is_payout"]:
            continue
        if not expense.created_by_id:
            continue

        if row["expense_type"] == EXPENSE_TYPE_SALARY_PAYOUT:
            payment_type = SettlementPayment.PAYMENT_TYPE_SALARY
        elif row["expense_type"] == EXPENSE_TYPE_REIMBURSEMENT_PAYOUT:
            payment_type = SettlementPayment.PAYMENT_TYPE_REIMBURSEMENT
        else:
            continue

        target_settlement = get_or_create_monthly_settlement(
            expense.expense_date.year,
            expense.expense_date.month,
        )
        payment, created = SettlementPayment.objects.get_or_create(
            legacy_coach_expense_id=expense.pk,
            defaults={
                "monthly_settlement": target_settlement,
                "coach": expense.created_by,
                "payment_type": payment_type,
                "amount": money(expense.amount),
                "paid_date": expense.expense_date,
                "note": row["plain_note"],
                "created_by": None,
            },
        )
        if created:
            synced += 1
            if payment_type == SettlementPayment.PAYMENT_TYPE_REIMBURSEMENT:
                allocate_reimbursement_fifo(payment)

    return synced


def payment_history_rows(settlement):
    rows = []
    payments = (
        SettlementPayment.objects.filter(monthly_settlement=settlement)
        .select_related("coach", "created_by")
        .order_by("-paid_date", "-id")
    )
    for payment in payments:
        rows.append(
            {
                "payment": payment,
                "expense": payment,
                "coach_name": display_name(payment.coach),
                "payout_type_label": payment.get_payment_type_display(),
                "amount": money(payment.amount),
                "plain_note": payment.note,
                "recorded_by_name": display_name(payment.created_by),
                "is_reversed": payment.is_reversed,
            }
        )
    return rows


def _current_payment_totals(settlement, coach):
    payments = SettlementPayment.objects.filter(
        monthly_settlement=settlement,
        coach=coach,
        is_reversed=False,
    )
    salary_paid = money(
        payments.filter(
            payment_type=SettlementPayment.PAYMENT_TYPE_SALARY
        ).aggregate(total=Sum("amount")).get("total")
    )
    reimbursement_paid = money(
        payments.filter(
            payment_type=SettlementPayment.PAYMENT_TYPE_REIMBURSEMENT
        ).aggregate(total=Sum("amount")).get("total")
    )
    return salary_paid, reimbursement_paid


def matching_active_payment(
    *, settlement, coach, payment_type, amount, paid_date, note
):
    return (
        SettlementPayment.objects.select_for_update()
        .filter(
            monthly_settlement=settlement,
            coach=coach,
            payment_type=payment_type,
            amount=amount,
            paid_date=paid_date,
            note=note,
            is_reversed=False,
        )
        .first()
    )


@transaction.atomic
def create_settlement_payment(
    *, settlement, coach, payment_type, amount, paid_date, note="", user=None
):
    """支払を月次行ロック下で冪等に登録する正式な入口。"""
    locked_settlement = MonthlySettlement.objects.select_for_update().get(
        pk=settlement.pk
    )
    if locked_settlement.is_closed:
        raise ValueError("締め済みの月には支払いを追加できません。")

    payment = matching_active_payment(
        settlement=locked_settlement,
        coach=coach,
        payment_type=payment_type,
        amount=amount,
        paid_date=paid_date,
        note=note,
    )
    if payment is not None:
        return payment, False, 0

    payment = SettlementPayment.objects.create(
        monthly_settlement=locked_settlement,
        coach=coach,
        payment_type=payment_type,
        amount=amount,
        paid_date=paid_date,
        note=note,
        created_by=user,
    )
    allocated = (
        allocate_reimbursement_fifo(payment)
        if payment_type == SettlementPayment.PAYMENT_TYPE_REIMBURSEMENT
        else 0
    )
    calculate_monthly_settlement(
        locked_settlement.year, locked_settlement.month, force=True
    )
    return payment, True, allocated


@transaction.atomic
def reverse_settlement_payment(*, settlement, payment_id, user, note=""):
    """支払取消と月次再計算を同一transactionで行う。"""
    locked_settlement = MonthlySettlement.objects.select_for_update().get(
        pk=settlement.pk
    )
    if locked_settlement.is_closed:
        raise ValueError("締め済みの月では支払いを取り消せません。")
    payment = SettlementPayment.objects.select_for_update().filter(
        pk=payment_id, monthly_settlement=locked_settlement
    ).first()
    if payment is None:
        raise SettlementPayment.DoesNotExist
    payment.reverse(user=user, note=note)
    calculate_monthly_settlement(
        locked_settlement.year, locked_settlement.month, force=True
    )
    return payment


@transaction.atomic
def close_monthly_settlement(*, year, month, user):
    result = calculate_monthly_settlement(year, month, force=True)
    settlement = MonthlySettlement.objects.select_for_update().get(
        pk=result["settlement"].pk
    )
    settlement.close(
        user=user,
        snapshot={
            "coach_rows": [
                {
                    "coach_id": row["coach"].pk,
                    "coach_name": row["coach_name"],
                    "salary_due": row["salary_due"],
                    "salary_paid": row["salary_paid"],
                    "unpaid_salary": row["unpaid_salary"],
                    "reimbursement_due": row["reimbursement_due"],
                    "reimbursement_paid": row["reimbursement_paid"],
                    "unpaid_reimbursement": row["unpaid_reimbursement"],
                }
                for row in result["coach_rows"]
            ],
            "cash_in_total": result["cash_in_total"],
            "cash_out_total": result["cash_out_total"],
            "closing_balance": result["company_balance"],
        },
    )
    return settlement


@transaction.atomic
def reopen_monthly_settlement(*, settlement, user):
    locked_settlement = MonthlySettlement.objects.select_for_update().get(
        pk=settlement.pk
    )
    locked_settlement.reopen(user=user)
    return locked_settlement


@transaction.atomic
def _calculate_monthly_settlement_base(year, month, *, force=False):
    month_start, next_month, _start_at, _end_at = aware_month_range(year, month)

    settlement = get_or_create_monthly_settlement(year, month)
    settlement = MonthlySettlement.objects.select_for_update().get(
        pk=settlement.pk,
    )

    if settlement.is_closed:
        coach_rows = []
        for saved in (
            CoachMonthlySettlement.objects.filter(monthly_settlement=settlement)
            .select_related("coach")
            .order_by("coach__full_name", "coach__username", "coach_id")
        ):
            coach_rows.append(
                {
                    "coach": saved.coach,
                    "coach_name": display_name(saved.coach),
                    "is_contractor_coach": saved.is_contractor_coach,
                    "reservation_count": saved.lesson_count,
                    "ticket_amount": saved.ticket_revenue,
                    "preopen_paid_amount": saved.preopen_paid_revenue,
                    "preopen_unpaid_amount": saved.preopen_unpaid_revenue,
                    "preopen_waived_amount": 0,
                    "stringing_amount": saved.stringing_revenue,
                    "contractor_hourly_wage": money(
                        getattr(saved.coach, "contractor_hourly_wage", 0)
                    ),
                    "contractor_work_minutes": money(
                        saved.calculation_snapshot.get("contractor_work_minutes")
                    ),
                    "contractor_work_hours_text": saved.calculation_snapshot.get(
                        "contractor_work_hours_text",
                        "0時間00分",
                    ),
                    "contractor_work_slot_count": money(
                        saved.calculation_snapshot.get("contractor_work_slot_count")
                    ),
                    "contractor_hourly_pay_amount": saved.contractor_work_amount,
                    "lesson_compensation_amount": saved.calculation_snapshot.get(
                        "lesson_compensation_amount",
                        0,
                    ),
                    "lesson_revenue_amount": (
                        saved.ticket_revenue + saved.preopen_paid_revenue
                    ),
                    "lesson_and_work_amount": saved.calculation_snapshot.get(
                        "lesson_and_work_amount",
                        0,
                    ),
                    "common_expense_share": saved.common_expense_share,
                    "court_cost_burden": money(
                        saved.calculation_snapshot.get("court_cost_burden")
                    ),
                    "rain_refund_burden": money(
                        saved.calculation_snapshot.get("rain_refund_burden")
                    ),
                    "ball_expense_burden": money(
                        saved.calculation_snapshot.get("ball_expense_burden")
                    ),
                    "other_expense_burden": money(
                        saved.calculation_snapshot.get("other_expense_burden")
                    ),
                    "contractor_cost_burden": money(
                        saved.calculation_snapshot.get("contractor_cost_burden")
                    ),
                    "ball_expense_reimbursement": money(
                        saved.calculation_snapshot.get(
                            "ball_expense_reimbursement"
                        )
                    ),
                    "rain_refund_reimbursement": money(
                        saved.calculation_snapshot.get(
                            "rain_refund_reimbursement"
                        )
                    ),
                    "wallet_reimbursement": money(
                        saved.calculation_snapshot.get("wallet_reimbursement")
                    ),
                    "wallet_balance_adjustment": money(
                        saved.calculation_snapshot.get("wallet_balance_adjustment")
                    ),
                    "negative_carry_in": money(
                        saved.calculation_snapshot.get("negative_carry_in")
                    ),
                    "unpaid_salary_carry_in": money(
                        saved.calculation_snapshot.get(
                            "unpaid_salary_carry_in"
                        )
                    ),
                    "wallet_final_entitlement": money(
                        saved.calculation_snapshot.get(
                            "wallet_final_entitlement",
                            saved.salary_due,
                        )
                    ),
                    "personal_reimbursement_due": saved.reimbursement_due,
                    "reimbursement_carry_in": saved.reimbursement_carry_in,
                    "reimbursement_current_month": saved.reimbursement_current_month,
                    "salary_due": saved.salary_due,
                    "salary_paid": saved.salary_paid,
                    "unpaid_salary": saved.salary_unpaid,
                    "reimbursement_due": saved.reimbursement_due,
                    "reimbursement_paid": saved.reimbursement_paid,
                    "unpaid_reimbursement": saved.reimbursement_unpaid,
                    "total_unpaid": (
                        saved.salary_unpaid + saved.reimbursement_unpaid
                    ),
                    "total_paid": saved.salary_paid + saved.reimbursement_paid,
                }
            )
        rain_refund_policy = dict(
            (settlement.calculation_snapshot or {}).get(
                "rain_refund_policy",
                {},
            )
        )
        return MonthlySettlementResult.from_mapping(
            {
                "settlement": settlement,
                "coach_rows": coach_rows,
                "is_closed": True,
                "rain_refund_pending_rows": rain_refund_policy.get(
                    "pending_rows",
                    [],
                ),
                "rain_refund_pending_total": money(
                    rain_refund_policy.get("pending_total")
                ),
                "rain_refunded_rows": rain_refund_policy.get(
                    "refunded_rows",
                    [],
                ),
                "rain_refunded_total": money(
                    rain_refund_policy.get("refunded_total")
                ),
            }
        )

    sync_legacy_payouts_through(next_month)

    monthly_data = load_monthly_settlement_data(
        month_start=month_start,
        next_month=next_month,
    )
    coaches = monthly_data["coaches"]
    coach_map = build_coach_map(
        coaches,
        display_name=display_name,
        money=money,
    )

    reservations = monthly_data["reservations"]

    active_regular_coach_ids, active_coach_ids = aggregate_reservations(
        reservations=reservations,
        coach_map=coach_map,
        reservation_model=Reservation,
        preopen_cash_price=PREOPEN_CASH_PRICE,
        is_preopen_cash_lesson_date=is_preopen_cash_lesson_date,
        money=money,
        execution_status_map=monthly_data["execution_status_map"],
    )

    stringing_orders = monthly_data["stringing_orders"]
    stringing_total = aggregate_stringing_orders(
        stringing_orders=stringing_orders,
        coach_map=coach_map,
        money=money,
    )

    all_expenses = monthly_data["all_expenses"]
    all_expense_meta_rows = [expense_meta_row(expense) for expense in all_expenses]

    expense_row_groups = classify_expense_rows(
        all_expense_meta_rows=all_expense_meta_rows,
        month_start=month_start,
        next_month=next_month,
        expense_type_common=EXPENSE_TYPE_COMMON,
        expense_type_personal=EXPENSE_TYPE_PERSONAL,
        approval_approved=EXPENSE_APPROVAL_APPROVED,
        approval_submitted=EXPENSE_APPROVAL_SUBMITTED,
    )
    approved_common_expense_rows = expense_row_groups[
        "approved_common_expense_rows"
    ]
    approved_personal_expense_rows = expense_row_groups[
        "approved_personal_expense_rows"
    ]
    submitted_personal_expense_rows = expense_row_groups[
        "submitted_personal_expense_rows"
    ]

    approved_common_expense_total = sum(
        money(row["expense"].amount) for row in approved_common_expense_rows
    )

    apply_reimbursement_amounts(
        approved_personal_expense_rows=approved_personal_expense_rows,
        coach_map=coach_map,
        month_start=month_start,
        next_month=next_month,
        expense_unpaid_amount=expense_unpaid_amount,
    )

    coach_calculation = calculate_coach_rows(
        coach_map=coach_map,
        active_regular_coach_ids=active_regular_coach_ids,
        approved_common_expense_total=approved_common_expense_total,
        settlement=settlement,
        current_payment_totals=_current_payment_totals,
    )
    coach_rows = coach_calculation["coach_rows"]
    contractor_hourly_pay_total = coach_calculation[
        "contractor_hourly_pay_total"
    ]
    common_expense_base_total = coach_calculation[
        "common_expense_base_total"
    ]
    common_expense_participant_count = coach_calculation[
        "common_expense_participant_count"
    ]
    per_coach_common_expense = coach_calculation[
        "per_coach_common_expense"
    ]

    totals = calculate_settlement_totals(
        coach_rows=coach_rows,
        ticket_purchases=monthly_data["ticket_purchases"],
        stringing_total=stringing_total,
        approved_common_expense_total=approved_common_expense_total,
        submitted_personal_expense_rows=submitted_personal_expense_rows,
        expense_approval_submitted=EXPENSE_APPROVAL_SUBMITTED,
        money=money,
    )
    preopen_paid_total = totals["preopen_paid_total"]
    preopen_unpaid_total = totals["preopen_unpaid_total"]
    ticket_amount_total = totals["ticket_amount_total"]
    ticket_purchase_total = totals["ticket_purchase_total"]
    salary_due_total = totals["salary_due_total"]
    reimbursement_due_total = totals["reimbursement_due_total"]
    salary_paid_total = totals["salary_paid_total"]
    reimbursement_paid_total = totals["reimbursement_paid_total"]
    unpaid_salary_total = totals["unpaid_salary_total"]
    unpaid_reimbursement_total = totals["unpaid_reimbursement_total"]
    pending_personal_reimbursement_total = totals[
        "pending_personal_reimbursement_total"
    ]
    cash_in_total = totals["cash_in_total"]
    cash_out_total = totals["cash_out_total"]

    persist_monthly_settlement(
        settlement=settlement,
        coach_rows=coach_rows,
        ticket_purchase_total=ticket_purchase_total,
        preopen_paid_total=preopen_paid_total,
        stringing_total=stringing_total,
        cash_in_total=cash_in_total,
        salary_paid_total=salary_paid_total,
        reimbursement_paid_total=reimbursement_paid_total,
        approved_common_expense_total=approved_common_expense_total,
        contractor_hourly_pay_total=contractor_hourly_pay_total,
        cash_out_total=cash_out_total,
        unpaid_salary_total=unpaid_salary_total,
        unpaid_reimbursement_total=unpaid_reimbursement_total,
        preopen_unpaid_total=preopen_unpaid_total,
        active_coach_ids=active_coach_ids,
        active_regular_coach_ids=active_regular_coach_ids,
        common_expense_participant_count=common_expense_participant_count,
        per_coach_common_expense=per_coach_common_expense,
        common_expense_base_total=common_expense_base_total,
    )

    return MonthlySettlementResult.from_mapping(
        {
            "settlement": settlement,
            "coach_rows": coach_rows,
            "is_closed": settlement.is_closed,
            "approved_common_expense_rows": approved_common_expense_rows,
            "approved_personal_expense_rows": approved_personal_expense_rows,
            "submitted_personal_expense_rows": submitted_personal_expense_rows,
            "preopen_paid_total": preopen_paid_total,
            "preopen_unpaid_total": preopen_unpaid_total,
            "ticket_amount_total": ticket_amount_total,
            "ticket_purchase_total": ticket_purchase_total,
            "stringing_total": stringing_total,
            "cash_in_total": cash_in_total,
            "approved_common_expense_total": approved_common_expense_total,
            "contractor_hourly_pay_total": contractor_hourly_pay_total,
            "common_expense_base_total": common_expense_base_total,
            "common_expense_participant_count": common_expense_participant_count,
            "salary_due_total": salary_due_total,
            "reimbursement_due_total": reimbursement_due_total,
            "salary_paid_total": salary_paid_total,
            "reimbursement_paid_total": reimbursement_paid_total,
            "unpaid_salary_total": unpaid_salary_total,
            "unpaid_reimbursement_total": unpaid_reimbursement_total,
            "pending_personal_reimbursement_total": (
                pending_personal_reimbursement_total
            ),
            "cash_out_total": cash_out_total,
            "company_balance": settlement.closing_balance,
            "opening_balance": settlement.opening_balance,
            "active_coach_count": len(active_coach_ids),
            "per_coach_common_expense": per_coach_common_expense,
            "payout_history_rows": payment_history_rows(settlement),
        }
    )


@transaction.atomic
def calculate_monthly_settlement(year, month, *, force=False):
    """月次精算の標準計算と会社財布ポリシーを一つの正式な入口で実行する。"""
    from .settlement_balance_policy import _apply_wallet_policy

    result = _calculate_monthly_settlement_base(
        year,
        month,
        force=force,
    )
    return MonthlySettlementResult.from_mapping(
        _apply_wallet_policy(result, year, month)
    )
