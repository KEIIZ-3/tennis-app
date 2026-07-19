import json
from datetime import date, datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .models import (
    CoachExpense,
    Reservation,
    StringingOrder,
    TicketPurchase,
    PREOPEN_CASH_PRICE,
    is_preopen_cash_lesson_date,
)
from .settlement_models import (
    CoachMonthlySettlement,
    ExpenseSettlementAllocation,
    MonthlySettlement,
    SettlementPayment,
)


EXPENSE_TYPE_PERSONAL = "personal"
EXPENSE_TYPE_COMMON = "common"
EXPENSE_TYPE_SALARY_PAYOUT = "salary_payout"
EXPENSE_TYPE_REIMBURSEMENT_PAYOUT = "reimbursement_payout"

EXPENSE_APPROVAL_SUBMITTED = "submitted"
EXPENSE_APPROVAL_APPROVED = "approved"

EXPENSE_NOTE_META_PREFIX = "__EXPENSE_META__"


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


def parse_expense_note(stored_note):
    default_meta = {
        "expense_type": EXPENSE_TYPE_COMMON,
        "receipt_status": "none",
        "receipt_check_status": "unchecked",
        "approval_status": EXPENSE_APPROVAL_APPROVED,
    }
    text = stored_note or ""
    if not text.startswith(EXPENSE_NOTE_META_PREFIX):
        return {
            **default_meta,
            "plain_note": text.strip(),
        }

    try:
        first_line, plain_note = text.split("\n", 1)
    except ValueError:
        first_line = text
        plain_note = ""

    meta_text = first_line[len(EXPENSE_NOTE_META_PREFIX):].strip()
    try:
        parsed = json.loads(meta_text or "{}")
    except Exception:
        parsed = {}

    return {
        **default_meta,
        **parsed,
        "plain_note": (plain_note or "").strip(),
    }


def expense_meta_row(expense):
    meta = parse_expense_note(expense.note)
    expense_type = str(meta.get("expense_type") or EXPENSE_TYPE_COMMON)
    approval_status = str(meta.get("approval_status") or EXPENSE_APPROVAL_APPROVED)
    is_payout = (
        str(meta.get("record_kind") or "") == "coach_payout"
        or expense_type in {
            EXPENSE_TYPE_SALARY_PAYOUT,
            EXPENSE_TYPE_REIMBURSEMENT_PAYOUT,
        }
    )
    type_labels = {
        EXPENSE_TYPE_PERSONAL: "本人立替",
        EXPENSE_TYPE_COMMON: "共通経費",
        EXPENSE_TYPE_SALARY_PAYOUT: "給与支払い",
        EXPENSE_TYPE_REIMBURSEMENT_PAYOUT: "本人立替精算支払い",
    }
    return {
        "expense": expense,
        "meta": meta,
        "plain_note": meta.get("plain_note", ""),
        "expense_type": expense_type,
        "expense_type_label": type_labels.get(expense_type, expense_type),
        "approval_status": approval_status,
        "is_payout": is_payout,
    }


def reservation_coaches_for_split(reservation):
    substitute = getattr(reservation, "substitute_coach", None)
    if substitute and getattr(substitute, "role", "") in ("coach", "contractor_coach"):
        return [substitute]

    fixed_lesson = getattr(reservation, "fixed_lesson", None)
    if fixed_lesson:
        try:
            coaches = [
                coach
                for coach in fixed_lesson.all_coaches()
                if coach
                and getattr(coach, "role", "") in ("coach", "contractor_coach")
            ]
            if coaches:
                return coaches
        except Exception:
            pass

    assigned = None
    try:
        assigned = reservation.assigned_coach()
    except Exception:
        assigned = (
            getattr(reservation, "substitute_coach", None)
            or getattr(reservation, "coach", None)
        )

    if assigned and getattr(assigned, "role", "") in ("coach", "contractor_coach"):
        return [assigned]
    return []


def reservation_duration_minutes(reservation):
    try:
        return max(
            int((reservation.end_at - reservation.start_at).total_seconds() // 60),
            0,
        )
    except Exception:
        return 0


def reservation_slot_key(reservation, coach):
    return (
        str(reservation.lesson_type or ""),
        str(getattr(reservation, "court_id", "") or ""),
        reservation.start_at.isoformat() if reservation.start_at else "",
        reservation.end_at.isoformat() if reservation.end_at else "",
        str(getattr(coach, "pk", "") or ""),
    )


def stringing_is_cancelled(order):
    raw = str(getattr(order, "status", "") or "").lower()
    return "cancel" in raw or "キャンセル" in raw


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


def _calculate_monthly_settlement_base(year, month, *, force=False):
    User = get_user_model()
    month_start, next_month, _start_at, _end_at = aware_month_range(year, month)

    sync_legacy_payouts_through(next_month)
    settlement = get_or_create_monthly_settlement(year, month)

    if settlement.is_closed and not force:
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
                    "wallet_reimbursement": money(
                        saved.calculation_snapshot.get("wallet_reimbursement")
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
        return {
            "settlement": settlement,
            "coach_rows": coach_rows,
            "is_closed": True,
        }

    coaches = list(
        User.objects.filter(role__in=("coach", "contractor_coach")).order_by(
            "full_name",
            "username",
            "id",
        )
    )
    coach_map = {}
    for coach in coaches:
        coach_map[coach.pk] = {
            "coach": coach,
            "coach_name": display_name(coach),
            "ticket_amount": 0,
            "preopen_paid_amount": 0,
            "preopen_unpaid_amount": 0,
            "preopen_waived_amount": 0,
            "stringing_amount": 0,
            "is_contractor_coach": getattr(coach, "role", "") == "contractor_coach",
            "contractor_hourly_wage": money(
                getattr(coach, "contractor_hourly_wage", 0)
            ),
            "contractor_work_minutes": 0,
            "contractor_work_slot_count": 0,
            "_lesson_slot_keys": set(),
            "contractor_hourly_pay_amount": 0,
            "lesson_compensation_amount": 0,
            "personal_reimbursement_due": 0,
            "reimbursement_carry_in": 0,
            "reimbursement_current_month": 0,
            "salary_paid": 0,
            "reimbursement_paid": 0,
            "common_expense_share": 0,
            "reservation_count": 0,
        }

    reservations = list(
        Reservation.objects.filter(
            start_at__date__gte=month_start,
            start_at__date__lt=next_month,
            status=Reservation.STATUS_ACTIVE,
        )
        .exclude(
            fixed_lesson__isnull=True,
            availability__note__startswith="固定レッスン:",
        )
        .select_related(
            "user",
            "coach",
            "substitute_coach",
            "court",
            "availability",
            "fixed_lesson",
            "fixed_lesson__coach",
            "fixed_lesson__coach_2",
            "fixed_lesson__coach_3",
        )
        .prefetch_related("ticket_consumptions__purchase")
        .order_by("start_at", "id")
    )

    active_regular_coach_ids = set()
    active_coach_ids = set()

    for reservation in reservations:
        split_coaches = reservation_coaches_for_split(reservation)
        if not split_coaches:
            continue

        denominator = max(len(split_coaches), 1)
        ticket_total = sum(
            money(consumption.unit_price_snapshot)
            * money(consumption.tickets_used)
            for consumption in reservation.ticket_consumptions.filter(
                refunded_at__isnull=True
            )
        )
        payment_amount = money(
            getattr(reservation, "payment_amount", 0) or PREOPEN_CASH_PRICE
        )
        is_preopen = (
            reservation.lesson_type == Reservation.LESSON_GENERAL
            and is_preopen_cash_lesson_date(reservation.start_at)
            and reservation.is_payment_tracking_required()
        )

        for coach in split_coaches:
            row = coach_map.get(coach.pk)
            if not row:
                continue

            active_coach_ids.add(coach.pk)
            if not row["is_contractor_coach"]:
                active_regular_coach_ids.add(coach.pk)

            slot_key = reservation_slot_key(reservation, coach)
            if slot_key not in row["_lesson_slot_keys"]:
                row["_lesson_slot_keys"].add(slot_key)
                row["reservation_count"] += 1
                if row["is_contractor_coach"]:
                    row["contractor_work_slot_count"] += 1
                    row["contractor_work_minutes"] += reservation_duration_minutes(
                        reservation
                    )

            if ticket_total > 0:
                row["ticket_amount"] += int(ticket_total / denominator)

            if is_preopen:
                split_amount = int(payment_amount / denominator)
                if reservation.payment_status == Reservation.PAYMENT_STATUS_PAID:
                    row["preopen_paid_amount"] += split_amount
                elif reservation.payment_status == Reservation.PAYMENT_STATUS_WAIVED:
                    row["preopen_waived_amount"] += split_amount
                else:
                    row["preopen_unpaid_amount"] += split_amount

    stringing_orders = list(
        StringingOrder.objects.filter(
            created_at__date__gte=month_start,
            created_at__date__lt=next_month,
        ).select_related("assigned_coach", "user")
    )
    stringing_total = 0
    for order in stringing_orders:
        if stringing_is_cancelled(order):
            continue
        amount = money(order.total_price())
        stringing_total += amount
        if getattr(order, "assigned_coach_id", None) in coach_map:
            coach_map[order.assigned_coach_id]["stringing_amount"] += amount

    all_expenses = list(
        CoachExpense.objects.filter(expense_date__lt=next_month)
        .select_related("created_by")
        .order_by("expense_date", "id")
    )
    all_expense_meta_rows = [expense_meta_row(expense) for expense in all_expenses]

    monthly_expense_meta_rows = [
        row
        for row in all_expense_meta_rows
        if month_start <= row["expense"].expense_date < next_month
    ]
    approved_common_expense_rows = [
        row
        for row in monthly_expense_meta_rows
        if not row["is_payout"]
        and row["approval_status"] == EXPENSE_APPROVAL_APPROVED
        and row["expense_type"] == EXPENSE_TYPE_COMMON
    ]
    approved_personal_expense_rows = [
        row
        for row in all_expense_meta_rows
        if not row["is_payout"]
        and row["approval_status"] == EXPENSE_APPROVAL_APPROVED
        and row["expense_type"] == EXPENSE_TYPE_PERSONAL
    ]
    submitted_personal_expense_rows = [
        row
        for row in all_expense_meta_rows
        if not row["is_payout"]
        and row["expense_type"] == EXPENSE_TYPE_PERSONAL
        and row["approval_status"]
        in (EXPENSE_APPROVAL_SUBMITTED, EXPENSE_APPROVAL_APPROVED)
    ]

    approved_common_expense_total = sum(
        money(row["expense"].amount) for row in approved_common_expense_rows
    )

    for row in approved_personal_expense_rows:
        expense = row["expense"]
        coach_id = getattr(expense, "created_by_id", None)
        if coach_id not in coach_map:
            continue
        unpaid = expense_unpaid_amount(expense, through_date=next_month - timedelta(days=1))
        if expense.expense_date < month_start:
            coach_map[coach_id]["reimbursement_carry_in"] += unpaid
        else:
            coach_map[coach_id]["reimbursement_current_month"] += unpaid

    for row in coach_map.values():
        if row["is_contractor_coach"]:
            row["contractor_hourly_pay_amount"] = int(
                row["contractor_work_minutes"]
                * row["contractor_hourly_wage"]
                / 60
            )
        row["contractor_work_hours_text"] = (
            f"{row['contractor_work_minutes'] // 60}時間"
            f"{row['contractor_work_minutes'] % 60:02d}分"
        )

    contractor_hourly_pay_total = sum(
        row["contractor_hourly_pay_amount"] for row in coach_map.values()
    )
    common_expense_base_total = (
        approved_common_expense_total + contractor_hourly_pay_total
    )
    common_expense_participant_count = len(active_regular_coach_ids)
    per_coach_common_expense = (
        int(common_expense_base_total / common_expense_participant_count)
        if common_expense_participant_count > 0
        else 0
    )

    coach_rows = []
    for row in coach_map.values():
        if (
            not row["is_contractor_coach"]
            and row["coach"].pk in active_regular_coach_ids
        ):
            row["common_expense_share"] = per_coach_common_expense
        else:
            row["common_expense_share"] = 0

        lesson_revenue_amount = (
            row["ticket_amount"] + row["preopen_paid_amount"]
        )
        if row["is_contractor_coach"]:
            lesson_compensation_amount = row["contractor_hourly_pay_amount"]
        else:
            lesson_compensation_amount = lesson_revenue_amount

        row["lesson_compensation_amount"] = lesson_compensation_amount
        lesson_and_work_amount = (
            lesson_compensation_amount + row["stringing_amount"]
        )
        salary_due = max(
            lesson_and_work_amount - row["common_expense_share"],
            0,
        )

        reimbursement_due = (
            row["reimbursement_carry_in"]
            + row["reimbursement_current_month"]
        )
        salary_paid, reimbursement_paid = _current_payment_totals(
            settlement,
            row["coach"],
        )
        unpaid_salary = max(salary_due - salary_paid, 0)
        unpaid_reimbursement = reimbursement_due

        row.update(
            {
                "lesson_revenue_amount": lesson_revenue_amount,
                "lesson_and_work_amount": lesson_and_work_amount,
                "salary_due": salary_due,
                "salary_paid": salary_paid,
                "unpaid_salary": unpaid_salary,
                "personal_reimbursement_due": reimbursement_due,
                "reimbursement_due": reimbursement_due,
                "reimbursement_paid": reimbursement_paid,
                "unpaid_reimbursement": unpaid_reimbursement,
                "total_unpaid": unpaid_salary + unpaid_reimbursement,
                "total_paid": salary_paid + reimbursement_paid,
            }
        )
        row.pop("_lesson_slot_keys", None)
        coach_rows.append(row)

    coach_rows.sort(key=lambda row: row["coach_name"])

    preopen_paid_total = sum(row["preopen_paid_amount"] for row in coach_rows)
    preopen_unpaid_total = sum(row["preopen_unpaid_amount"] for row in coach_rows)
    ticket_amount_total = sum(row["ticket_amount"] for row in coach_rows)

    ticket_purchase_total = sum(
        money(purchase.total_tickets) * money(purchase.unit_price)
        for purchase in TicketPurchase.objects.filter(
            purchased_at__date__gte=month_start,
            purchased_at__date__lt=next_month,
        )
    )

    salary_due_total = sum(row["salary_due"] for row in coach_rows)
    reimbursement_due_total = sum(
        row["reimbursement_due"] for row in coach_rows
    )
    salary_paid_total = sum(row["salary_paid"] for row in coach_rows)
    reimbursement_paid_total = sum(
        row["reimbursement_paid"] for row in coach_rows
    )
    unpaid_salary_total = sum(row["unpaid_salary"] for row in coach_rows)
    unpaid_reimbursement_total = sum(
        row["unpaid_reimbursement"] for row in coach_rows
    )

    cash_in_total = (
        preopen_paid_total + ticket_purchase_total + stringing_total
    )
    cash_out_total = (
        salary_paid_total
        + reimbursement_paid_total
        + approved_common_expense_total
    )

    settlement.ticket_cash_in = ticket_purchase_total
    settlement.preopen_cash_in = preopen_paid_total
    settlement.stringing_cash_in = stringing_total
    settlement.cash_in_total = cash_in_total
    settlement.salary_cash_out = salary_paid_total
    settlement.reimbursement_cash_out = reimbursement_paid_total
    settlement.common_expense_cash_out = approved_common_expense_total
    settlement.contractor_cash_out = contractor_hourly_pay_total
    settlement.cash_out_total = cash_out_total
    settlement.unpaid_salary_total = unpaid_salary_total
    settlement.unpaid_reimbursement_total = unpaid_reimbursement_total
    settlement.uncollected_revenue_total = preopen_unpaid_total
    settlement.recalculate_closing_balance()
    settlement.updated_at = timezone.now()
    settlement.save()

    saved_coach_ids = []
    for row in coach_rows:
        saved, _created = CoachMonthlySettlement.objects.update_or_create(
            monthly_settlement=settlement,
            coach=row["coach"],
            defaults={
                "is_contractor_coach": row["is_contractor_coach"],
                "lesson_count": row["reservation_count"],
                "ticket_revenue": row["ticket_amount"],
                "preopen_paid_revenue": row["preopen_paid_amount"],
                "preopen_unpaid_revenue": row["preopen_unpaid_amount"],
                "stringing_revenue": row["stringing_amount"],
                "contractor_work_amount": row["contractor_hourly_pay_amount"],
                "common_expense_share": row["common_expense_share"],
                "reimbursement_carry_in": row["reimbursement_carry_in"],
                "reimbursement_current_month": row[
                    "reimbursement_current_month"
                ],
                "reimbursement_due": row["reimbursement_due"],
                "salary_due": row["salary_due"],
                "salary_paid": row["salary_paid"],
                "salary_unpaid": row["unpaid_salary"],
                "reimbursement_paid": row["reimbursement_paid"],
                "reimbursement_unpaid": row["unpaid_reimbursement"],
                "calculation_snapshot": {
                    "contractor_work_minutes": row[
                        "contractor_work_minutes"
                    ],
                    "contractor_work_hours_text": row[
                        "contractor_work_hours_text"
                    ],
                    "contractor_work_slot_count": row[
                        "contractor_work_slot_count"
                    ],
                    "lesson_compensation_amount": row[
                        "lesson_compensation_amount"
                    ],
                    "lesson_and_work_amount": row[
                        "lesson_and_work_amount"
                    ],
                },
                "updated_at": timezone.now(),
            },
        )
        saved_coach_ids.append(saved.pk)

    CoachMonthlySettlement.objects.filter(
        monthly_settlement=settlement
    ).exclude(pk__in=saved_coach_ids).delete()

    settlement.calculation_snapshot = {
        "active_coach_count": len(active_coach_ids),
        "active_regular_coach_ids": sorted(active_regular_coach_ids),
        "common_expense_participant_count": common_expense_participant_count,
        "per_coach_common_expense": per_coach_common_expense,
        "common_expense_base_total": common_expense_base_total,
        "contractor_hourly_pay_total": contractor_hourly_pay_total,
    }
    settlement.updated_at = timezone.now()
    settlement.save(update_fields=["calculation_snapshot", "updated_at"])

    pending_personal_reimbursement_total = sum(
        money(row["expense"].amount)
        for row in submitted_personal_expense_rows
        if row["approval_status"] == EXPENSE_APPROVAL_SUBMITTED
    )

    return {
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


def calculate_monthly_settlement(year, month, *, force=False):
    """月次精算の標準計算と会社財布ポリシーを一つの正式な入口で実行する。"""
    from .settlement_balance_policy import _apply_wallet_policy

    result = _calculate_monthly_settlement_base(
        year,
        month,
        force=force,
    )
    return _apply_wallet_policy(result, year, month)
