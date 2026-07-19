from collections import defaultdict
from datetime import date

from django.contrib.auth import get_user_model
from django.db.models import Sum
from django.utils import timezone


MAIN_COACH_NAMES = (
    "飯塚研太朗",
    "清水峻平",
    "井上春佳",
)

WEEKDAY_COURT_RATE_PER_HOUR = 900
WEEKEND_HOLIDAY_COURT_RATE_PER_HOUR = 1200
LIGHTING_RATE_PER_HOUR = 400

EXPENSE_TYPE_PERSONAL = "personal"
EXPENSE_TYPE_COMMON = "common"
EXPENSE_TYPE_COURT_TRANSFER = "court_transfer"
EXPENSE_APPROVAL_APPROVED = "approved"
EXPENSE_NOTE_META_PREFIX = "__EXPENSE_META__"
COURT_TRANSFER_RECORD_KIND = "court_transfer"

try:
    import jpholiday
except ImportError:  # pragma: no cover
    jpholiday = None


def _money(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
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


def main_coaches():
    User = get_user_model()
    users_by_name = {
        _display_name(user): user
        for user in User.objects.filter(role="coach").order_by("id")
    }
    return [
        users_by_name[coach_name]
        for coach_name in MAIN_COACH_NAMES
        if coach_name in users_by_name
    ]


def _month_range(year, month):
    start_date = date(int(year), int(month), 1)
    if int(month) == 12:
        end_date = date(int(year) + 1, 1, 1)
    else:
        end_date = date(int(year), int(month) + 1, 1)
    return start_date, end_date


def _local_datetime(value):
    if value and timezone.is_aware(value):
        return timezone.localtime(value)
    return value


def _parse_expense_note(stored_note):
    import json

    defaults = {
        "expense_type": EXPENSE_TYPE_COMMON,
        "receipt_status": "none",
        "receipt_check_status": "unchecked",
        "approval_status": EXPENSE_APPROVAL_APPROVED,
    }
    text = stored_note or ""

    if not text.startswith(EXPENSE_NOTE_META_PREFIX):
        return {
            **defaults,
            "plain_note": text.strip(),
        }

    try:
        first_line, plain_note = text.split("\n", 1)
    except ValueError:
        first_line = text
        plain_note = ""

    raw_json = first_line[len(EXPENSE_NOTE_META_PREFIX):].strip()
    try:
        parsed = json.loads(raw_json or "{}")
    except Exception:
        parsed = {}

    return {
        **defaults,
        **parsed,
        "plain_note": (plain_note or "").strip(),
    }


def _is_japanese_holiday(target_date):
    if target_date.weekday() >= 5:
        return True

    if jpholiday is None:
        return False

    try:
        return bool(jpholiday.is_holiday(target_date))
    except Exception:
        return False


def _lighting_start_hour(target_date):
    if target_date.month in (5, 6, 7, 8):
        return 19
    if target_date.month in (3, 4, 9):
        return 18
    return 17


def _overlap_hours(start_at, end_at, boundary_hour):
    start_local = _local_datetime(start_at)
    end_local = _local_datetime(end_at)

    if not start_local or not end_local or end_local <= start_local:
        return 0

    boundary = start_local.replace(
        hour=boundary_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    overlap_start = max(start_local, boundary)
    if end_local <= overlap_start:
        return 0

    return max(
        int((end_local - overlap_start).total_seconds() // 3600),
        0,
    )


def _reservation_duration_hours(reservation):
    try:
        seconds = (reservation.end_at - reservation.start_at).total_seconds()
        return max(int(seconds // 3600), 0)
    except Exception:
        return 0


def _automatic_court_cost(reservation):
    start_local = _local_datetime(reservation.start_at)
    if not start_local:
        return 0

    duration_hours = _reservation_duration_hours(reservation)
    if duration_hours <= 0:
        return 0

    base_rate = (
        WEEKEND_HOLIDAY_COURT_RATE_PER_HOUR
        if _is_japanese_holiday(start_local.date())
        else WEEKDAY_COURT_RATE_PER_HOUR
    )
    court_count = max(int(getattr(reservation, "court_count", 1) or 1), 1)
    base_cost = base_rate * duration_hours * court_count

    lighting_hours = _overlap_hours(
        reservation.start_at,
        reservation.end_at,
        _lighting_start_hour(start_local.date()),
    )
    lighting_cost = LIGHTING_RATE_PER_HOUR * lighting_hours * court_count

    return base_cost + lighting_cost


def _reservation_coaches(reservation):
    substitute = getattr(reservation, "substitute_coach", None)
    if substitute and getattr(substitute, "role", "") in (
        "coach",
        "contractor_coach",
    ):
        return [substitute]

    fixed_lesson = getattr(reservation, "fixed_lesson", None)
    if fixed_lesson:
        try:
            coaches = [
                coach
                for coach in fixed_lesson.all_coaches()
                if coach
                and getattr(coach, "role", "") in (
                    "coach",
                    "contractor_coach",
                )
            ]
            if coaches:
                return coaches
        except Exception:
            pass

    try:
        assigned = reservation.assigned_coach()
    except Exception:
        assigned = (
            getattr(reservation, "substitute_coach", None)
            or getattr(reservation, "coach", None)
        )

    if assigned and getattr(assigned, "role", "") in (
        "coach",
        "contractor_coach",
    ):
        return [assigned]

    return []


def _split_amount(amount, coach_ids):
    unique_ids = list(dict.fromkeys(coach_id for coach_id in coach_ids if coach_id))
    if not unique_ids:
        return {}

    base_amount, remainder = divmod(_money(amount), len(unique_ids))
    return {
        coach_id: base_amount + (1 if index < remainder else 0)
        for index, coach_id in enumerate(unique_ids)
    }


def _slot_key_for_reservation(reservation):
    start_local = _local_datetime(reservation.start_at)
    end_local = _local_datetime(reservation.end_at)

    if not start_local or not end_local:
        return ""

    court = getattr(reservation, "court", None)
    court_type = str(getattr(court, "court_type", "") or "").strip()
    if court_type:
        facility_key = f"facility:{court_type}"
    else:
        court_name = str(getattr(court, "name", "") or court or "").strip()
        facility_key = (
            f"facility_name:{court_name}"
            if court_name
            else "facility:unknown"
        )

    return (
        f"{start_local.date().isoformat()}|"
        f"{facility_key}|"
        f"{start_local:%H:%M}|"
        f"{end_local:%H:%M}"
    )


def _is_court_expense(expense):
    try:
        from .models import CoachExpense

        return expense.category == CoachExpense.CATEGORY_COURT
    except Exception:
        return False


def _approved_monthly_expenses(month_start, next_month):
    from .models import CoachExpense

    rows = []
    queryset = (
        CoachExpense.objects.filter(
            expense_date__gte=month_start,
            expense_date__lt=next_month,
        )
        .select_related("created_by")
        .order_by("expense_date", "id")
    )

    for expense in queryset:
        meta = _parse_expense_note(expense.note)
        if meta.get("approval_status") != EXPENSE_APPROVAL_APPROVED:
            continue

        expense_type = str(meta.get("expense_type") or EXPENSE_TYPE_COMMON)
        if expense_type not in (
            EXPENSE_TYPE_PERSONAL,
            EXPENSE_TYPE_COMMON,
            EXPENSE_TYPE_COURT_TRANSFER,
        ):
            continue

        rows.append(
            {
                "expense": expense,
                "meta": meta,
                "expense_type": expense_type,
                "amount": _money(expense.amount),
                "payer_id": getattr(expense, "created_by_id", None),
                "is_court": _is_court_expense(expense),
                "slot_key": str(meta.get("court_refund_slot_key") or "").strip(),
            }
        )

    return rows


def _eligible_reservations(year, month):
    from .models import Reservation
    from .lesson_execution_storage import read_status_map
    from .settlement_models import MonthlySettlement

    month_start, next_month = _month_range(year, month)
    now = timezone.now()

    reservations = list(
        Reservation.objects.filter(
            start_at__date__gte=month_start,
            start_at__date__lt=next_month,
            status=Reservation.STATUS_ACTIVE,
            end_at__lte=now,
        )
        .select_related(
            "coach",
            "substitute_coach",
            "court",
            "availability",
            "fixed_lesson",
            "fixed_lesson__coach",
            "fixed_lesson__coach_2",
            "fixed_lesson__coach_3",
        )
        .order_by("start_at", "id")
    )
    settlement = MonthlySettlement.objects.filter(
        year=int(year),
        month=int(month),
    ).first()
    if settlement is None:
        return []
    return _held_execution_reservations(
        reservations,
        read_status_map(settlement),
    )


def _execution_slot_key(reservation):
    fixed_lesson = getattr(reservation, "fixed_lesson", None)
    if fixed_lesson is not None:
        start_local = _local_datetime(reservation.start_at)
        return f"fixed:{fixed_lesson.pk}:{start_local.date().isoformat()}"

    availability = getattr(reservation, "availability", None)
    if availability is None:
        return ""
    return f"availability:{availability.pk}"


def _held_execution_reservations(reservations, status_map):
    eligible_by_slot = {}
    for reservation in reservations:
        slot_key = _execution_slot_key(reservation)
        if not slot_key:
            continue
        entry = status_map.get(slot_key) or {}
        if entry.get("status") != "held":
            continue
        eligible_by_slot.setdefault(slot_key, reservation)
    return list(eligible_by_slot.values())


def _court_transfer_allocation(
    expense_rows,
    eligible_coach_ids,
    *,
    main_coach_ids=None,
    contractor_coach_ids=None,
):
    eligible_coach_id_set = set(eligible_coach_ids)
    main_coach_id_list = [
        coach_id
        for coach_id in (main_coach_ids or [])
        if coach_id in eligible_coach_id_set
    ]
    contractor_coach_id_set = set(contractor_coach_ids or [])
    burden_by_coach = defaultdict(int)
    reimbursement_by_coach = defaultdict(int)
    detail_rows = []

    for row in expense_rows:
        meta = row["meta"]
        if meta.get("record_kind") != COURT_TRANSFER_RECORD_KIND:
            continue

        using_coach_ids = []
        for value in meta.get("using_coach_ids") or []:
            try:
                coach_id = int(value)
            except (TypeError, ValueError):
                continue
            if (
                coach_id in eligible_coach_id_set
                and coach_id not in using_coach_ids
            ):
                using_coach_ids.append(coach_id)

        amount = max(_money(row["amount"]), 0)
        if amount <= 0 or not using_coach_ids:
            continue

        contractor_only = bool(using_coach_ids) and all(
            coach_id in contractor_coach_id_set for coach_id in using_coach_ids
        )
        burden_target_ids = main_coach_id_list if contractor_only else using_coach_ids
        if not burden_target_ids:
            continue

        for coach_id, allocated in _split_amount(
            amount,
            burden_target_ids,
        ).items():
            burden_by_coach[coach_id] += allocated

        try:
            payer_id = int(meta.get("payer_coach_id"))
        except (TypeError, ValueError):
            payer_id = None
        if payer_id in eligible_coach_id_set:
            reimbursement_by_coach[payer_id] += amount

        detail_rows.append(
            {
                "expense_id": row["expense"].pk,
                "amount": amount,
                "payer_id": payer_id,
                "burden_target_ids": burden_target_ids,
                "burden_rule": (
                    "業務委託コーチのみのためメインコーチ3人で均等負担"
                    if contractor_only
                    else "登録された利用コーチで均等負担"
                ),
                "is_court_transfer": True,
            }
        )

    return {
        "burden_by_coach": dict(burden_by_coach),
        "reimbursement_by_coach": dict(reimbursement_by_coach),
        "detail_rows": detail_rows,
        "expense_ids": {row["expense_id"] for row in detail_rows},
        "total": sum(row["amount"] for row in detail_rows),
    }


def _build_court_cost_policy(
    year,
    month,
    main_coach_ids,
    eligible_coach_ids,
    contractor_coach_ids,
):
    month_start, next_month = _month_range(year, month)
    reservations = _eligible_reservations(year, month)
    expenses = _approved_monthly_expenses(month_start, next_month)

    transfer = _court_transfer_allocation(
        expenses,
        eligible_coach_ids,
        main_coach_ids=main_coach_ids,
        contractor_coach_ids=contractor_coach_ids,
    )
    transfer_expense_ids = transfer["expense_ids"]

    court_expenses_by_slot = defaultdict(list)
    unlinked_court_expenses = []

    for row in expenses:
        if not row["is_court"]:
            continue
        if row["expense"].pk in transfer_expense_ids:
            continue
        if row["slot_key"]:
            court_expenses_by_slot[row["slot_key"]].append(row)
        else:
            unlinked_court_expenses.append(row)

    burden_by_coach = defaultdict(int, transfer["burden_by_coach"])
    reimbursement_by_coach = defaultdict(
        int,
        transfer["reimbursement_by_coach"],
    )
    detail_rows = list(transfer["detail_rows"])
    unmatched_expected_total = 0
    used_expense_ids = set(transfer_expense_ids)

    for reservation in reservations:
        expected_cost = _automatic_court_cost(reservation)
        slot_key = _slot_key_for_reservation(reservation)
        linked_expenses = court_expenses_by_slot.get(slot_key, [])

        matched_expense = None
        for candidate in linked_expenses:
            expense_id = candidate["expense"].pk
            if expense_id not in used_expense_ids:
                matched_expense = candidate
                used_expense_ids.add(expense_id)
                break

        if matched_expense:
            finalized_cost = matched_expense["amount"]
            payer_id = matched_expense["payer_id"]
            if payer_id:
                reimbursement_by_coach[payer_id] += finalized_cost
            is_finalized = True
        else:
            finalized_cost = 0
            payer_id = None
            is_finalized = False
            unmatched_expected_total += expected_cost

        coaches = _reservation_coaches(reservation)
        regular_main_ids = [
            coach.pk
            for coach in coaches
            if coach.pk in main_coach_ids
            and getattr(coach, "role", "") == "coach"
        ]
        contractor_only = bool(coaches) and all(
            getattr(coach, "role", "") == "contractor_coach"
            for coach in coaches
        )

        if contractor_only:
            burden_targets = list(main_coach_ids)
            burden_rule = "業務委託コーチのみのためメインコーチ3人負担"
        elif regular_main_ids:
            burden_targets = regular_main_ids
            burden_rule = "担当メインコーチ負担"
        else:
            burden_targets = []
            burden_rule = "負担先未確定"

        if finalized_cost and burden_targets:
            for coach_id, allocated in _split_amount(
                finalized_cost,
                burden_targets,
            ).items():
                burden_by_coach[coach_id] += allocated

        detail_rows.append(
            {
                "reservation_id": reservation.pk,
                "start_at": reservation.start_at.isoformat(),
                "end_at": reservation.end_at.isoformat(),
                "slot_key": slot_key,
                "expected_cost": expected_cost,
                "finalized_cost": finalized_cost,
                "payer_id": payer_id,
                "burden_target_ids": burden_targets,
                "burden_rule": burden_rule,
                "is_finalized": is_finalized,
            }
        )

    unused_registered_total = 0
    for row in expenses:
        if not row["is_court"]:
            continue
        if row["expense"].pk in used_expense_ids:
            continue
        unused_registered_total += row["amount"]

    return {
        "burden_by_coach": dict(burden_by_coach),
        "reimbursement_by_coach": dict(reimbursement_by_coach),
        "detail_rows": detail_rows,
        "finalized_court_cost_total": sum(burden_by_coach.values()),
        "court_reimbursement_total": sum(reimbursement_by_coach.values()),
        "unmatched_expected_total": unmatched_expected_total,
        "unused_registered_total": unused_registered_total,
        "unlinked_court_expense_ids": [
            row["expense"].pk for row in unlinked_court_expenses
        ],
        "court_transfer_total": transfer["total"],
    }


def _build_other_expense_policy(year, month, main_coach_ids):
    month_start, next_month = _month_range(year, month)
    expenses = _approved_monthly_expenses(month_start, next_month)

    burden_by_coach = defaultdict(int)
    reimbursement_by_coach = defaultdict(int)
    detail_rows = []

    for row in expenses:
        if row["is_court"]:
            continue

        amount = row["amount"]
        payer_id = row["payer_id"]
        if payer_id:
            reimbursement_by_coach[payer_id] += amount

        if row["expense_type"] == EXPENSE_TYPE_COMMON:
            target_ids = list(main_coach_ids)
            rule = "メインコーチ3人均等負担"
        else:
            target_ids = [payer_id] if payer_id else []
            rule = "本人負担"

        for coach_id, allocated in _split_amount(amount, target_ids).items():
            burden_by_coach[coach_id] += allocated

        detail_rows.append(
            {
                "expense_id": row["expense"].pk,
                "amount": amount,
                "payer_id": payer_id,
                "burden_target_ids": target_ids,
                "burden_rule": rule,
            }
        )

    return {
        "burden_by_coach": dict(burden_by_coach),
        "reimbursement_by_coach": dict(reimbursement_by_coach),
        "detail_rows": detail_rows,
        "expense_total": sum(row["amount"] for row in detail_rows),
        "reimbursement_total": sum(reimbursement_by_coach.values()),
    }


def _active_salary_payment_total(settlement, coach):
    from .settlement_models import SettlementPayment

    result = SettlementPayment.objects.filter(
        monthly_settlement=settlement,
        coach=coach,
        payment_type=SettlementPayment.PAYMENT_TYPE_SALARY,
        is_reversed=False,
    ).aggregate(total=Sum("amount"))
    return _money(result.get("total"))


def _apply_wallet_policy(result, year, month):
    from .settlement_models import CoachMonthlySettlement

    settlement = result.get("settlement")
    if settlement is None or settlement.is_closed:
        return result

    coach_rows = list(result.get("coach_rows") or [])
    if not coach_rows:
        return result

    main_coach_list = main_coaches()
    main_coach_ids = [coach.pk for coach in main_coach_list]
    main_coach_id_set = set(main_coach_ids)
    eligible_coach_ids = [
        getattr(row.get("coach"), "pk", None)
        for row in coach_rows
        if getattr(row.get("coach"), "pk", None) is not None
    ]

    court_policy = _build_court_cost_policy(
        year,
        month,
        main_coach_ids,
        eligible_coach_ids,
        [
            getattr(row.get("coach"), "pk", None)
            for row in coach_rows
            if row.get("is_contractor_coach")
            and getattr(row.get("coach"), "pk", None) is not None
        ],
    )
    other_expense_policy = _build_other_expense_policy(
        year,
        month,
        main_coach_ids,
    )

    contractor_pay_total = sum(
        _money(row.get("contractor_hourly_pay_amount"))
        for row in coach_rows
        if row.get("is_contractor_coach")
    )
    contractor_share_by_main = _split_amount(
        contractor_pay_total,
        main_coach_ids,
    )

    total_company_revenue = sum(
        _money(row.get("ticket_amount"))
        + _money(row.get("preopen_paid_amount"))
        + _money(row.get("stringing_amount"))
        for row in coach_rows
    )

    row_by_coach_id = {
        getattr(row.get("coach"), "pk", None): row
        for row in coach_rows
    }

    final_total_before_adjustment = 0

    for row in coach_rows:
        coach = row.get("coach")
        coach_id = getattr(coach, "pk", None)
        is_contractor = bool(row.get("is_contractor_coach"))

        lesson_revenue = (
            _money(row.get("ticket_amount"))
            + _money(row.get("preopen_paid_amount"))
        )
        stringing_revenue = _money(row.get("stringing_amount"))

        court_burden = _money(
            court_policy["burden_by_coach"].get(coach_id)
        )
        other_expense_burden = _money(
            other_expense_policy["burden_by_coach"].get(coach_id)
        )
        contractor_burden = _money(
            contractor_share_by_main.get(coach_id)
        )

        court_reimbursement = _money(
            court_policy["reimbursement_by_coach"].get(coach_id)
        )
        other_expense_reimbursement = _money(
            other_expense_policy["reimbursement_by_coach"].get(coach_id)
        )
        reimbursement_total = (
            court_reimbursement + other_expense_reimbursement
        )

        if is_contractor:
            earned_amount = (
                _money(row.get("contractor_hourly_pay_amount"))
                + stringing_revenue
            )
            burden_total = 0
        else:
            earned_amount = lesson_revenue + stringing_revenue
            burden_total = (
                court_burden
                + other_expense_burden
                + contractor_burden
            )

        final_entitlement = (
            earned_amount
            + reimbursement_total
            - burden_total
        )

        row.update(
            {
                "is_main_coach": coach_id in main_coach_id_set,
                "company_revenue_contribution": (
                    lesson_revenue + stringing_revenue
                ),
                "court_cost_burden": court_burden,
                "other_expense_burden": other_expense_burden,
                "contractor_cost_burden": contractor_burden,
                "total_cost_burden": burden_total,
                "court_reimbursement": court_reimbursement,
                "other_expense_reimbursement": (
                    other_expense_reimbursement
                ),
                "wallet_reimbursement": reimbursement_total,
                "wallet_earned_amount": earned_amount,
                "wallet_final_entitlement": final_entitlement,
                "wallet_balance_adjustment": 0,
            }
        )
        final_total_before_adjustment += final_entitlement

    wallet_difference = total_company_revenue - final_total_before_adjustment

    adjustment_by_coach = {}
    if wallet_difference != 0 and main_coach_ids:
        positive_contributions = {
            coach_id: max(
                _money(
                    row_by_coach_id.get(coach_id, {}).get(
                        "company_revenue_contribution"
                    )
                ),
                0,
            )
            for coach_id in main_coach_ids
        }
        contribution_total = sum(positive_contributions.values())

        if contribution_total > 0:
            allocated = 0
            for index, coach_id in enumerate(main_coach_ids):
                if index == len(main_coach_ids) - 1:
                    adjustment = wallet_difference - allocated
                else:
                    adjustment = int(
                        wallet_difference
                        * positive_contributions[coach_id]
                        / contribution_total
                    )
                    allocated += adjustment
                adjustment_by_coach[coach_id] = adjustment
        else:
            adjustment_by_coach = _split_amount(
                wallet_difference,
                main_coach_ids,
            )

        for coach_id, adjustment in adjustment_by_coach.items():
            row = row_by_coach_id.get(coach_id)
            if row is None:
                continue
            row["wallet_balance_adjustment"] = adjustment
            row["wallet_final_entitlement"] += adjustment

    salary_due_total = 0
    salary_paid_total = 0
    unpaid_salary_total = 0
    negative_carry_total = 0

    for row in coach_rows:
        coach = row.get("coach")
        final_entitlement = _money(row.get("wallet_final_entitlement"))
        salary_paid = _active_salary_payment_total(settlement, coach)

        salary_due = max(final_entitlement, 0)
        closing_balance = final_entitlement - salary_paid
        unpaid_salary = max(closing_balance, 0)
        negative_carry = max(-closing_balance, 0)

        row.update(
            {
                "salary_due": salary_due,
                "salary_paid": salary_paid,
                "unpaid_salary": unpaid_salary,
                "negative_carry": negative_carry,
                "closing_compensation_balance": closing_balance,
                "personal_reimbursement_due": _money(
                    row.get("wallet_reimbursement")
                ),
                "reimbursement_due": _money(
                    row.get("wallet_reimbursement")
                ),
                "unpaid_reimbursement": 0,
                "total_unpaid": unpaid_salary,
                "total_paid": salary_paid,
                "common_expense_share": _money(
                    row.get("total_cost_burden")
                ),
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
                    "wallet_policy": True,
                    "is_main_coach": bool(
                        row.get("is_main_coach")
                    ),
                    "company_revenue_contribution": _money(
                        row.get("company_revenue_contribution")
                    ),
                    "court_cost_burden": _money(
                        row.get("court_cost_burden")
                    ),
                    "other_expense_burden": _money(
                        row.get("other_expense_burden")
                    ),
                    "contractor_cost_burden": _money(
                        row.get("contractor_cost_burden")
                    ),
                    "total_cost_burden": _money(
                        row.get("total_cost_burden")
                    ),
                    "court_reimbursement": _money(
                        row.get("court_reimbursement")
                    ),
                    "other_expense_reimbursement": _money(
                        row.get("other_expense_reimbursement")
                    ),
                    "wallet_reimbursement": _money(
                        row.get("wallet_reimbursement")
                    ),
                    "wallet_earned_amount": _money(
                        row.get("wallet_earned_amount")
                    ),
                    "wallet_balance_adjustment": _money(
                        row.get("wallet_balance_adjustment")
                    ),
                    "wallet_final_entitlement": final_entitlement,
                    "closing_compensation_balance": closing_balance,
                    "negative_carry": negative_carry,
                }
            )

            saved_row.common_expense_share = _money(
                row.get("total_cost_burden")
            )
            saved_row.reimbursement_due = _money(
                row.get("wallet_reimbursement")
            )
            saved_row.reimbursement_current_month = _money(
                row.get("wallet_reimbursement")
            )
            saved_row.reimbursement_carry_in = 0
            saved_row.salary_due = salary_due
            saved_row.salary_paid = salary_paid
            saved_row.salary_unpaid = unpaid_salary
            saved_row.reimbursement_paid = 0
            saved_row.reimbursement_unpaid = 0
            saved_row.calculation_snapshot = snapshot
            saved_row.updated_at = timezone.now()
            saved_row.save(
                update_fields=[
                    "common_expense_share",
                    "reimbursement_due",
                    "reimbursement_current_month",
                    "reimbursement_carry_in",
                    "salary_due",
                    "salary_paid",
                    "salary_unpaid",
                    "reimbursement_paid",
                    "reimbursement_unpaid",
                    "calculation_snapshot",
                    "updated_at",
                ]
            )

        salary_due_total += salary_due
        salary_paid_total += salary_paid
        unpaid_salary_total += unpaid_salary
        negative_carry_total += negative_carry

    settlement.opening_balance = 0
    settlement.cash_in_total = total_company_revenue
    settlement.ticket_cash_in = _money(
        result.get("ticket_amount_total")
    )
    settlement.preopen_cash_in = _money(
        result.get("preopen_paid_total")
    )
    settlement.stringing_cash_in = _money(
        result.get("stringing_total")
    )
    settlement.salary_cash_out = salary_paid_total
    settlement.reimbursement_cash_out = 0
    settlement.common_expense_cash_out = 0
    settlement.contractor_cash_out = contractor_pay_total
    settlement.cash_out_total = salary_paid_total
    settlement.unpaid_salary_total = unpaid_salary_total
    settlement.unpaid_reimbursement_total = 0
    settlement.closing_balance = max(
        total_company_revenue - salary_paid_total,
        0,
    )

    settlement_snapshot = dict(settlement.calculation_snapshot or {})
    settlement_snapshot.update(
        {
            "wallet_policy": True,
            "company_internal_reserve": 0,
            "company_revenue_definition": (
                "ticket_consumption + collected_cash + stringing"
            ),
            "main_coach_names": list(MAIN_COACH_NAMES),
            "main_coach_ids": main_coach_ids,
            "total_company_revenue": total_company_revenue,
            "contractor_pay_total": contractor_pay_total,
            "contractor_share_by_main": contractor_share_by_main,
            "court_policy": court_policy,
            "other_expense_policy": other_expense_policy,
            "wallet_difference_before_adjustment": wallet_difference,
            "wallet_adjustment_by_coach": adjustment_by_coach,
            "negative_carry_total": negative_carry_total,
        }
    )
    settlement.calculation_snapshot = settlement_snapshot
    settlement.updated_at = timezone.now()
    settlement.save()

    result.update(
        {
            "coach_rows": coach_rows,
            "cash_in_total": total_company_revenue,
            "company_balance": settlement.closing_balance,
            "opening_balance": 0,
            "salary_due_total": salary_due_total,
            "salary_paid_total": salary_paid_total,
            "unpaid_salary_total": unpaid_salary_total,
            "reimbursement_due_total": 0,
            "reimbursement_paid_total": 0,
            "unpaid_reimbursement_total": 0,
            "cash_out_total": salary_paid_total,
            "approved_common_expense_total": (
                other_expense_policy["expense_total"]
            ),
            "contractor_hourly_pay_total": contractor_pay_total,
            "common_expense_base_total": (
                other_expense_policy["expense_total"]
                + contractor_pay_total
            ),
            "common_expense_participant_count": len(main_coach_ids),
            "court_cost_total": court_policy[
                "finalized_court_cost_total"
            ],
            "court_cost_expected_unregistered_total": court_policy[
                "unmatched_expected_total"
            ],
            "court_cost_registered_unused_total": court_policy[
                "unused_registered_total"
            ],
            "wallet_policy": True,
            "wallet_revenue_total": total_company_revenue,
            "wallet_remaining_payable": settlement.closing_balance,
            "negative_carry_total": negative_carry_total,
        }
    )
    return result
