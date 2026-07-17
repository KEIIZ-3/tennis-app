from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal, ROUND_FLOOR

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.utils import timezone

MAIN_COACH_NAMES = ("飯塚研太朗", "清水峻平", "井上春佳")
WEEKDAY_RATE = 900
HOLIDAY_RATE = 1200
LIGHTING_RATE = 400

try:
    import jpholiday  # type: ignore
except Exception:  # pragma: no cover
    jpholiday = None


def money(value):
    try:
        return int(value or 0)
    except Exception:
        return 0


def name(user):
    if not user:
        return ""
    try:
        return str(user.display_name() or "").strip()
    except Exception:
        return str(getattr(user, "full_name", "") or getattr(user, "username", "") or "").strip()


def main_coaches():
    User = get_user_model()
    found = {name(user): user for user in User.objects.filter(role="coach").order_by("id")}
    return [found[value] for value in MAIN_COACH_NAMES if value in found]


def month_range(year, month):
    start = date(int(year), int(month), 1)
    end = date(int(year) + 1, 1, 1) if int(month) == 12 else date(int(year), int(month) + 1, 1)
    return start, end


def local(value):
    return timezone.localtime(value) if value and timezone.is_aware(value) else value


def split_amount(amount, coach_ids):
    coach_ids = [coach_id for coach_id in coach_ids if coach_id]
    if not coach_ids:
        return {}
    base, remainder = divmod(money(amount), len(coach_ids))
    return {
        coach_id: base + (1 if index < remainder else 0)
        for index, coach_id in enumerate(coach_ids)
    }


def is_holiday(target):
    if target.weekday() >= 5:
        return True
    if jpholiday is None:
        return False
    try:
        return bool(jpholiday.is_holiday(target))
    except Exception:
        return False


def lighting_start_hour(target):
    if target.month in (5, 6, 7, 8):
        return 19
    if target.month in (3, 4, 9):
        return 18
    return 17


def hours(start_at, end_at):
    if not start_at or not end_at or end_at <= start_at:
        return Decimal("0")
    return Decimal(str((end_at - start_at).total_seconds())) / Decimal("3600")


def court_cost(start_at, end_at, court_count=1):
    start_at = local(start_at)
    end_at = local(end_at)
    if not start_at or not end_at:
        return 0
    base_rate = HOLIDAY_RATE if is_holiday(start_at.date()) else WEEKDAY_RATE
    light_start = datetime.combine(start_at.date(), time(lighting_start_hour(start_at.date())))
    if timezone.is_aware(start_at):
        light_start = timezone.make_aware(light_start, timezone.get_current_timezone())
    light_hours = hours(max(start_at, light_start), end_at)
    amount = (
        hours(start_at, end_at) * Decimal(base_rate)
        + light_hours * Decimal(LIGHTING_RATE)
    ) * Decimal(max(money(court_count), 1))
    return int(amount.quantize(Decimal("1"), rounding=ROUND_FLOOR))


def facility_key(court):
    court_type = str(getattr(court, "court_type", "") or "").strip()
    if court_type:
        return f"facility:{court_type}"
    court_name = str(getattr(court, "name", "") or court or "").strip()
    return f"facility_name:{court_name}" if court_name else "facility:unknown"


def court_slot_key(reservation):
    start_at = local(reservation.start_at)
    end_at = local(reservation.end_at)
    return (
        f"{start_at.date().isoformat()}|{facility_key(reservation.court)}|"
        f"{start_at:%H:%M}|{end_at:%H:%M}"
    )


def reservation_group_key(reservation):
    if getattr(reservation, "availability_id", None):
        return ("availability", reservation.availability_id)
    return (
        reservation.lesson_type,
        reservation.court_id,
        reservation.start_at,
        reservation.end_at,
        getattr(reservation, "fixed_lesson_id", None),
    )


def assigned_coaches(reservation):
    from .settlement_service import reservation_coaches_for_split

    result = []
    seen = set()
    for coach in reservation_coaches_for_split(reservation):
        if coach.pk not in seen:
            seen.add(coach.pk)
            result.append(coach)
    return result


def approved_expenses(year, month):
    from .settlement_service import expense_meta_row
    from .models import CoachExpense

    start, end = month_range(year, month)
    rows = []
    for expense in (
        CoachExpense.objects.filter(expense_date__gte=start, expense_date__lt=end)
        .select_related("created_by")
        .order_by("expense_date", "id")
    ):
        meta_row = expense_meta_row(expense)
        if meta_row["is_payout"] or meta_row["approval_status"] != "approved":
            continue
        rows.append((expense, meta_row))
    return rows


def build_expense_ledger(year, month, mains):
    from .models import CoachExpense, Reservation

    start, end = month_range(year, month)
    main_ids = {coach.pk for coach in mains}
    approved = approved_expenses(year, month)
    court_expenses = defaultdict(list)
    other_expenses = []
    for expense, meta_row in approved:
        if expense.category == CoachExpense.CATEGORY_COURT:
            key = str((meta_row.get("meta") or {}).get("court_refund_slot_key") or "").strip()
            if key:
                court_expenses[key].append(expense)
        else:
            other_expenses.append(expense)

    grouped = {}
    reservations = (
        Reservation.objects.filter(
            start_at__date__gte=start,
            start_at__date__lt=end,
            status=Reservation.STATUS_ACTIVE,
        )
        .select_related(
            "coach", "substitute_coach", "court", "availability", "fixed_lesson",
            "fixed_lesson__coach", "fixed_lesson__coach_2", "fixed_lesson__coach_3",
        )
        .order_by("start_at", "id")
    )
    for reservation in reservations:
        grouped.setdefault(reservation_group_key(reservation), reservation)

    burden = defaultdict(int)
    reimbursement = defaultdict(int)
    court_rows = []
    unmatched = []
    total_court = 0

    for reservation in grouped.values():
        availability = getattr(reservation, "availability", None)
        amount = court_cost(
            reservation.start_at,
            reservation.end_at,
            getattr(availability, "court_count", 1) or 1,
        )
        key = court_slot_key(reservation)
        linked = court_expenses.get(key, [])
        payer = linked[0].created_by if linked else None
        coaches = assigned_coaches(reservation)
        assigned_main = [coach for coach in coaches if coach.pk in main_ids]
        contractors = [coach for coach in coaches if getattr(coach, "role", "") == "contractor_coach"]
        burden_coaches = assigned_main if assigned_main else (list(mains) if contractors else [])

        if amount <= 0 or not payer or payer.pk not in main_ids or not burden_coaches:
            if amount > 0:
                unmatched.append({
                    "slot_key": key,
                    "lesson": f"{local(reservation.start_at):%Y/%m/%d %H:%M}〜{local(reservation.end_at):%H:%M}",
                    "court": str(reservation.court),
                    "amount": amount,
                    "reason": "承認済みコート経費の立替者、または負担コーチが未確定です。",
                })
            continue

        shares = split_amount(amount, [coach.pk for coach in burden_coaches])
        for coach_id, share in shares.items():
            burden[coach_id] += share
        reimbursement[payer.pk] += amount
        total_court += amount
        court_rows.append({
            "slot_key": key,
            "lesson": f"{local(reservation.start_at):%Y/%m/%d %H:%M}〜{local(reservation.end_at):%H:%M}",
            "court": str(reservation.court),
            "amount": amount,
            "payer_id": payer.pk,
            "payer_name": name(payer),
            "burden_names": [name(coach) for coach in burden_coaches],
            "burden_shares": {str(key): value for key, value in shares.items()},
            "contractor_only": bool(contractors and not assigned_main),
        })

    other_total = 0
    for expense in other_expenses:
        other_total += money(expense.amount)
        if expense.created_by_id in main_ids:
            reimbursement[expense.created_by_id] += money(expense.amount)

    return {
        "court_total": total_court,
        "court_burden": dict(burden),
        "reimbursement": dict(reimbursement),
        "other_total": other_total,
        "court_rows": court_rows,
        "unmatched": unmatched,
    }


def previous_balance(coach, year, month):
    from .settlement_models import CoachMonthlySettlement

    previous_year, previous_month = (int(year) - 1, 12) if int(month) == 1 else (int(year), int(month) - 1)
    saved = CoachMonthlySettlement.objects.filter(
        monthly_settlement__year=previous_year,
        monthly_settlement__month=previous_month,
        coach=coach,
    ).first()
    if not saved:
        return 0
    snapshot = saved.calculation_snapshot or {}
    return money(snapshot.get("closing_compensation_balance", saved.salary_unpaid))


def salary_paid(settlement, coach):
    from .settlement_models import SettlementPayment

    return money(SettlementPayment.objects.filter(
        monthly_settlement=settlement,
        coach=coach,
        payment_type=SettlementPayment.PAYMENT_TYPE_SALARY,
        is_reversed=False,
    ).aggregate(total=Sum("amount")).get("total"))


def allocate_residual(rows, amount):
    eligible = [row for row in rows if row["is_main_coach"]]
    if amount <= 0 or not eligible:
        return
    weights = [max(money(row["gross_contribution"]), 0) for row in eligible]
    if sum(weights) <= 0:
        weights = [1] * len(eligible)
    remaining = amount
    total_weight = sum(weights)
    for index, row in enumerate(eligible):
        share = remaining if index == len(eligible) - 1 else int(amount * weights[index] / total_weight)
        remaining -= share
        row["wallet_residual_allocation"] += share
        row["balance_before_payment"] += share


def apply_wallet_result(result, year, month):
    from .settlement_models import CoachMonthlySettlement

    settlement = result.get("settlement")
    rows = list(result.get("coach_rows") or [])
    if not settlement or settlement.is_closed or not rows:
        return result

    mains = main_coaches()
    if not mains:
        return result
    main_ids = {coach.pk for coach in mains}
    ledger = build_expense_ledger(year, month, mains)
    contractor_total = money(result.get("contractor_hourly_pay_total"))
    shared_total = ledger["other_total"] + contractor_total
    shared = split_amount(shared_total, [coach.pk for coach in mains])

    lesson_revenue = money(result.get("ticket_amount_total")) + money(result.get("preopen_paid_total"))
    stringing_revenue = money(result.get("stringing_total"))
    wallet_revenue = lesson_revenue + stringing_revenue

    for row in rows:
        coach = row["coach"]
        coach_id = coach.pk
        row["is_main_coach"] = coach_id in main_ids
        row["gross_contribution"] = (
            money(row.get("ticket_amount"))
            + money(row.get("preopen_paid_amount"))
            + money(row.get("stringing_amount"))
        )
        row["court_expense_burden"] = money(ledger["court_burden"].get(coach_id))
        row["shared_operating_expense"] = money(shared.get(coach_id)) if row["is_main_coach"] else 0
        row["court_expense_reimbursement"] = money(ledger["reimbursement"].get(coach_id))
        row["wallet_residual_allocation"] = 0

        if row.get("is_contractor_coach"):
            current = money(row.get("contractor_hourly_pay_amount")) + money(row.get("stringing_amount"))
        elif row["is_main_coach"]:
            current = row["gross_contribution"] - row["court_expense_burden"] - row["shared_operating_expense"]
        else:
            current = money(row.get("stringing_amount"))

        opening = previous_balance(coach, year, month)
        row["current_month_compensation"] = current
        row["opening_compensation_balance"] = opening
        row["balance_before_payment"] = opening + current
        row["salary_paid"] = salary_paid(settlement, coach)
        row["common_expense_share"] = row["court_expense_burden"] + row["shared_operating_expense"]

        existing_due = money(row.get("reimbursement_due"))
        auto_due = row["court_expense_reimbursement"]
        row["reimbursement_due"] = existing_due + auto_due
        row["personal_reimbursement_due"] = row["reimbursement_due"]
        row["reimbursement_current_month"] = money(row.get("reimbursement_current_month")) + auto_due

    fixed_outflow = sum(money(row.get("reimbursement_due")) for row in rows) + contractor_total
    available_main = max(wallet_revenue - fixed_outflow, 0)
    positive_main = sum(max(money(row["balance_before_payment"]), 0) for row in rows if row["is_main_coach"])

    if positive_main > available_main:
        eligible = [row for row in rows if row["is_main_coach"] and row["balance_before_payment"] > 0]
        remaining = available_main
        for index, row in enumerate(eligible):
            capped = remaining if index == len(eligible) - 1 else int(available_main * row["balance_before_payment"] / positive_main)
            remaining -= capped
            row["balance_before_payment"] = capped
    else:
        allocate_residual(rows, available_main - positive_main)

    totals = defaultdict(int)
    for row in rows:
        balance = money(row["balance_before_payment"])
        paid = money(row["salary_paid"])
        closing = balance - paid
        row["salary_due"] = max(balance, 0)
        row["unpaid_salary"] = max(closing, 0)
        row["negative_carry"] = max(-closing, 0)
        row["closing_compensation_balance"] = closing
        row["unpaid_reimbursement"] = max(money(row["reimbursement_due"]) - money(row.get("reimbursement_paid")), 0)
        row["total_unpaid"] = row["unpaid_salary"] + row["unpaid_reimbursement"]
        row["total_paid"] = paid + money(row.get("reimbursement_paid"))

        saved = CoachMonthlySettlement.objects.filter(monthly_settlement=settlement, coach=row["coach"]).first()
        if saved:
            snapshot = dict(saved.calculation_snapshot or {})
            snapshot.update({
                "wallet_policy": "zero_reserve_monthly_wallet",
                "is_main_coach": row["is_main_coach"],
                "gross_contribution": row["gross_contribution"],
                "court_expense_burden": row["court_expense_burden"],
                "court_expense_reimbursement": row["court_expense_reimbursement"],
                "shared_operating_expense": row["shared_operating_expense"],
                "wallet_residual_allocation": row["wallet_residual_allocation"],
                "current_month_compensation": row["current_month_compensation"],
                "opening_compensation_balance": row["opening_compensation_balance"],
                "balance_before_payment": balance,
                "closing_compensation_balance": closing,
                "negative_carry": row["negative_carry"],
            })
            saved.common_expense_share = row["common_expense_share"]
            saved.reimbursement_due = row["reimbursement_due"]
            saved.reimbursement_current_month = row["reimbursement_current_month"]
            saved.salary_due = row["salary_due"]
            saved.salary_paid = paid
            saved.salary_unpaid = row["unpaid_salary"]
            saved.reimbursement_paid = money(row.get("reimbursement_paid"))
            saved.reimbursement_unpaid = row["unpaid_reimbursement"]
            saved.calculation_snapshot = snapshot
            saved.updated_at = timezone.now()
            saved.save()

        totals["salary_due"] += row["salary_due"]
        totals["salary_paid"] += paid
        totals["reimbursement_due"] += row["reimbursement_due"]
        totals["reimbursement_paid"] += money(row.get("reimbursement_paid"))
        totals["unpaid_salary"] += row["unpaid_salary"]
        totals["unpaid_reimbursement"] += row["unpaid_reimbursement"]
        totals["negative_carry"] += row["negative_carry"]

    recorded_out = totals["salary_paid"] + totals["reimbursement_paid"]
    wallet_balance = max(wallet_revenue - recorded_out, 0)
    settlement.opening_balance = 0
    settlement.ticket_cash_in = money(result.get("ticket_amount_total"))
    settlement.preopen_cash_in = money(result.get("preopen_paid_total"))
    settlement.stringing_cash_in = stringing_revenue
    settlement.cash_in_total = wallet_revenue
    settlement.salary_cash_out = totals["salary_paid"]
    settlement.reimbursement_cash_out = totals["reimbursement_paid"]
    settlement.common_expense_cash_out = 0
    settlement.contractor_cash_out = contractor_total
    settlement.cash_out_total = recorded_out
    settlement.unpaid_salary_total = totals["unpaid_salary"]
    settlement.unpaid_reimbursement_total = totals["unpaid_reimbursement"]
    settlement.closing_balance = wallet_balance
    settlement.calculation_snapshot = {
        **(settlement.calculation_snapshot or {}),
        "wallet_policy": "zero_reserve_monthly_wallet",
        "company_is_wallet": True,
        "main_coach_names": list(MAIN_COACH_NAMES),
        "main_coach_ids": [coach.pk for coach in mains],
        "lesson_wallet_revenue": lesson_revenue,
        "stringing_wallet_revenue": stringing_revenue,
        "wallet_revenue_total": wallet_revenue,
        "court_cost_total": ledger["court_total"],
        "court_ledger_rows": ledger["court_rows"],
        "court_unmatched_rows": ledger["unmatched"],
        "shared_operating_expense_total": shared_total,
        "contractor_hourly_pay_total": contractor_total,
        "available_for_main_compensation": available_main,
        "negative_carry_total": totals["negative_carry"],
    }
    settlement.updated_at = timezone.now()
    settlement.save()

    result.update({
        "coach_rows": rows,
        "company_is_wallet": True,
        "wallet_policy": "zero_reserve_monthly_wallet",
        "opening_balance": 0,
        "cash_in_total": wallet_revenue,
        "company_balance": wallet_balance,
        "lesson_wallet_revenue": lesson_revenue,
        "stringing_wallet_revenue": stringing_revenue,
        "wallet_revenue_total": wallet_revenue,
        "court_cost_total": ledger["court_total"],
        "court_ledger_rows": ledger["court_rows"],
        "court_unmatched_rows": ledger["unmatched"],
        "approved_common_expense_total": ledger["other_total"],
        "common_expense_base_total": shared_total,
        "common_expense_participant_count": len(mains),
        "per_coach_common_expense": int(shared_total / max(len(mains), 1)),
        "salary_due_total": totals["salary_due"],
        "salary_paid_total": totals["salary_paid"],
        "reimbursement_due_total": totals["reimbursement_due"],
        "reimbursement_paid_total": totals["reimbursement_paid"],
        "unpaid_salary_total": totals["unpaid_salary"],
        "unpaid_reimbursement_total": totals["unpaid_reimbursement"],
        "negative_carry_total": totals["negative_carry"],
    })
    return result


def apply_payment_guard():
    from .settlement_models import SettlementPayment

    if getattr(SettlementPayment.save, "_wallet_guard_applied", False):
        return
    original_save = SettlementPayment.save

    def guarded_save(self, *args, **kwargs):
        if self._state.adding and not self.legacy_coach_expense_id:
            from .settlement_service import calculate_monthly_settlement

            result = calculate_monthly_settlement(self.monthly_settlement.year, self.monthly_settlement.month)
            row = next((item for item in result.get("coach_rows", []) if item["coach"].pk == self.coach_id), None)
            if self.payment_type == SettlementPayment.PAYMENT_TYPE_SALARY:
                limit = money(row.get("unpaid_salary")) if row else 0
            else:
                limit = money(row.get("unpaid_reimbursement")) if row else 0
            if money(self.amount) > limit:
                raise ValidationError(f"支払可能上限は{limit:,}円です。会社の財布をマイナスにはできません。")
            remaining = max(
                money(result.get("wallet_revenue_total"))
                - money(result.get("salary_paid_total"))
                - money(result.get("reimbursement_paid_total")),
                0,
            )
            if money(self.amount) > remaining:
                raise ValidationError(f"会社の財布残高を超えています。現在の支払可能上限は{remaining:,}円です。")
        return original_save(self, *args, **kwargs)

    guarded_save._wallet_guard_applied = True
    guarded_save._original_save = original_save
    SettlementPayment.save = guarded_save


def apply_settlement_balance_policy():
    from . import settlement_service

    if getattr(settlement_service.calculate_monthly_settlement, "_wallet_policy_applied", False):
        apply_payment_guard()
        return
    original = settlement_service.calculate_monthly_settlement

    def calculate(year, month, *, force=False):
        return apply_wallet_result(original(year, month, force=force), year, month)

    calculate._wallet_policy_applied = True
    calculate._original_calculate = original
    settlement_service.calculate_monthly_settlement = calculate
    apply_payment_guard()
