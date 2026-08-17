from collections import defaultdict
from datetime import date

from django.contrib.auth import get_user_model
from django.db.models import Q, Sum
from django.utils import timezone

from .models import MAIN_COACH_NAMES
from .ball_expense_allocation import (
    held_participant_count_by_coach,
    split_amount_by_participant_count,
)
from .settlement_coach_calculation import calculate_coach_wallets
from .court_cost_allocation import allocate_court_cost
from .settlement_expense_distribution import build_expense_distribution_policies

WEEKDAY_COURT_RATE_PER_HOUR = 900
WEEKEND_HOLIDAY_COURT_RATE_PER_HOUR = 1200
LIGHTING_RATE_PER_HOUR = 400

EXPENSE_TYPE_PERSONAL = "personal"
EXPENSE_TYPE_COMMON = "common"
EXPENSE_TYPE_COURT_TRANSFER = "court_transfer"
EXPENSE_APPROVAL_APPROVED = "approved"
EXPENSE_APPROVAL_REFUND_PENDING = "refund_pending"
EXPENSE_APPROVAL_REFUNDED = "refunded"
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


def _company_cash_in_total(result, coach_rows):
    """Cash received this month; ticket consumption is intentionally excluded."""
    return _money(result.get("ticket_purchase_total")) + sum(
        _money(row.get("preopen_paid_amount"))
        + _money(row.get("stringing_amount"))
        for row in coach_rows
    )


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
    def normalized_name(value):
        return "".join(str(value or "").replace("\u3000", " ").split())

    users_by_name = {}
    for user in User.objects.all().order_by("id"):
        display_name = normalized_name(_display_name(user))
        full_name = normalized_name(getattr(user, "full_name", ""))
        for name in (display_name, full_name):
            if name:
                users_by_name[name] = user

    return [
        users_by_name[normalized_name(coach_name)]
        for coach_name in MAIN_COACH_NAMES
        if normalized_name(coach_name) in users_by_name
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


def _split_amount_by_lesson_count(amount, coach_ids, lesson_count_by_coach):
    """合計額を担当人数比で四捨五入し、差額は最少担当者から調整する。"""
    return split_amount_by_participant_count(
        amount,
        coach_ids,
        lesson_count_by_coach,
        money=_money,
        split_evenly=_split_amount,
    )


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


def _ball_expense_amount_for_month(expense, meta, month_start, next_month):
    """複数月分の購入総額から、指定精算月だけのボール代を返す。"""
    amount = _money(expense.amount)
    period_start = getattr(expense, "settlement_period_start", None)
    period_end = getattr(expense, "settlement_period_end", None)
    if not (period_start and period_end):
        return None
    target_month = month_start.replace(day=1)
    if not (period_start <= target_month <= period_end):
        return None

    try:
        month_count = (
            (period_end.year - period_start.year) * 12
            + period_end.month
            - period_start.month
            + 1
        )
        month_index = (
            (target_month.year - period_start.year) * 12
            + month_start.month
            - period_start.month
        )
        base_amount, remainder = divmod(amount, month_count)
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return None

    return base_amount + (1 if month_index < remainder else 0)


def _approved_monthly_expenses(month_start, next_month):
    from .models import CoachExpense

    rows = []
    queryset = (
        CoachExpense.objects.filter(
            Q(expense_date__gte=month_start, expense_date__lt=next_month)
            | Q(
                category=CoachExpense.CATEGORY_BALL,
                settlement_period_start__lte=month_start,
                settlement_period_end__gte=month_start,
            )
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

        amount = _money(expense.amount)
        if expense.category == CoachExpense.CATEGORY_BALL:
            amount = _ball_expense_amount_for_month(
                expense,
                meta,
                month_start,
                next_month,
            )
            if amount is None:
                continue

        rows.append(
            {
                "expense": expense,
                "meta": meta,
                "expense_type": expense_type,
                "amount": amount,
                "payer_id": getattr(expense, "created_by_id", None),
                "is_court": _is_court_expense(expense),
                "slot_key": str(meta.get("court_refund_slot_key") or "").strip(),
            }
        )

    return rows


def _rain_refund_policy(year, month, main_coach_ids):
    """返金確認済みだけを回収者から支払者への振替として精算する。"""
    from .models import RainRefund

    month_start, next_month = _month_range(year, month)
    main_coach_id_set = set(main_coach_ids)
    burden_by_coach = defaultdict(int)
    reimbursement_by_coach = defaultdict(int)
    pending_rows = []
    refunded_rows = []

    refunds = (
        RainRefund.objects.filter(
            lesson_date__gte=month_start,
            lesson_date__lt=next_month,
        )
        .select_related(
            "expense",
            "booking_account_coach",
            "collection_coach",
            "debit_coach",
            "payer_coach",
        )
        .order_by("lesson_date", "id")
    )
    for refund in refunds:
        amount = max(_money(refund.amount), 0)
        row = {
            "expense_id": refund.expense_id,
            "expense_date": refund.lesson_date.isoformat(),
            "amount": amount,
            "lesson_label": refund.lesson_label,
            "account_name": refund.account_name,
            "collection_coach_name": (
                refund.collection_coach.display_name()
                if refund.collection_coach_id
                else ""
            ),
            "payer_coach_name": refund.payer_coach.display_name(),
        }
        if refund.status == RainRefund.STATUS_PENDING:
            pending_rows.append(row)
            continue

        debit_coach_id = refund.debit_coach_id
        payer_coach_id = refund.payer_coach_id
        if (
            amount <= 0
            or debit_coach_id not in main_coach_id_set
            or payer_coach_id not in main_coach_id_set
        ):
            continue
        burden_by_coach[debit_coach_id] += amount
        reimbursement_by_coach[payer_coach_id] += amount
        refunded_rows.append(
            {
                **row,
                "debit_coach_id": debit_coach_id,
                "payer_coach_id": payer_coach_id,
            }
        )

    return {
        "burden_by_coach": dict(burden_by_coach),
        "reimbursement_by_coach": dict(reimbursement_by_coach),
        "pending_rows": pending_rows,
        "refunded_rows": refunded_rows,
        "pending_total": sum(row["amount"] for row in pending_rows),
        "refunded_total": sum(row["amount"] for row in refunded_rows),
    }


def _monthly_execution_reservations_and_status(year, month):
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
        .exclude(
            fixed_lesson__isnull=True,
            availability__note__startswith="固定レッスン:",
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
        return reservations, {}
    return reservations, read_status_map(settlement)


def _eligible_reservations(year, month):
    reservations, status_map = _monthly_execution_reservations_and_status(
        year,
        month,
    )
    return _held_execution_reservations(
        reservations,
        status_map,
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


def _held_participant_count_by_coach(year, month, coach_ids):
    """終了済みかつ雨天中止でないレッスンの参加人数を担当別に集計する。"""
    reservations, status_map = _monthly_execution_reservations_and_status(
        year,
        month,
    )
    return held_participant_count_by_coach(
        reservations,
        status_map,
        eligible_coach_ids=coach_ids,
        execution_slot_key=_execution_slot_key,
        reservation_coaches=_reservation_coaches,
    )


def _court_transfer_allocation(
    expense_rows,
    eligible_coach_ids,
    *,
    main_coach_ids=None,
    contractor_coach_ids=None,
    excluded_availability_ids=None,
):
    eligible_coach_id_set = set(eligible_coach_ids)
    configured_main_coach_ids = (
        main_coach_ids
        if main_coach_ids is not None
        else [
            coach_id
            for coach_id in eligible_coach_ids
            if coach_id not in set(contractor_coach_ids or [])
        ]
    )
    main_coach_id_list = [
        coach_id
        for coach_id in configured_main_coach_ids
        if coach_id in eligible_coach_id_set
    ]
    contractor_coach_id_set = set(contractor_coach_ids or [])
    burden_by_coach = defaultdict(int)
    reimbursement_by_coach = defaultdict(int)
    detail_rows = []
    excluded_availability_id_set = set(excluded_availability_ids or [])
    from .court_transfer_service import current_court_transfer_rows

    rows_without_availability, canonical_rows_by_availability = (
        current_court_transfer_rows(expense_rows)
    )
    canonical_rows_by_availability = {
        availability_id: row
        for availability_id, row in canonical_rows_by_availability.items()
        if availability_id not in excluded_availability_id_set
    }

    canonical_rows = [
        *rows_without_availability,
        *canonical_rows_by_availability.values(),
    ]
    for row in canonical_rows:
        meta = row["meta"]

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

        allocation = allocate_court_cost(
            amount,
            using_coach_ids,
            main_coach_ids=main_coach_id_list,
            contractor_coach_ids=contractor_coach_id_set,
        )
        burden_target_ids = allocation["burden_target_ids"]
        if not burden_target_ids:
            continue

        for coach_id, allocated in allocation["burden_by_coach"].items():
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
                "date": str(getattr(row["expense"], "expense_date", "") or ""),
                "start_at": meta.get("start_at") or "",
                "end_at": meta.get("end_at") or "",
                "court": meta.get("court_name") or meta.get("account_name") or "",
                "court_count": _money(meta.get("court_count")) or None,
                "calculated_cost": None,
                "registered_cost": amount,
                "canonical_cost": amount,
                "amount": amount,
                "payer_id": payer_id,
                "using_coach_ids": using_coach_ids,
                "burden_target_ids": burden_target_ids,
                "burden_rule": (
                    "業務委託コーチのみのためメインコーチ3人で均等負担"
                    if allocation["rule"] == "contractor_only"
                    else "登録された利用コーチで均等負担"
                ),
                "is_court_transfer": True,
                "execution_status": "registered_transfer",
                "canceled": False,
                "rain_canceled": False,
                "included_reason": "canonical availability-linked court transfer",
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
    from .models import RainRefund

    month_start, next_month = _month_range(year, month)
    reservations = _eligible_reservations(year, month)
    expenses = _approved_monthly_expenses(month_start, next_month)
    rain_refund_availability_ids = set(
        RainRefund.objects.filter(
            lesson_date__gte=month_start,
            lesson_date__lt=next_month,
            availability_id__isnull=False,
        ).values_list("availability_id", flat=True)
    )

    transfer = _court_transfer_allocation(
        expenses,
        eligible_coach_ids,
        main_coach_ids=main_coach_ids,
        contractor_coach_ids=contractor_coach_ids,
        excluded_availability_ids=rain_refund_availability_ids,
    )
    transfer_expense_ids = transfer["expense_ids"]

    # 新方式のコート代は availability_id を正規の紐づけキーとする。
    # 金額0円の「登録不要」も含め、旧方式の日付・施設・時刻照合へ流さない。
    transfer_availability_ids = set()
    transfer_slot_keys = set()
    for row in expenses:
        meta = row["meta"]
        if meta.get("record_kind") != COURT_TRANSFER_RECORD_KIND:
            continue
        try:
            availability_id = int(meta.get("availability_id"))
        except (TypeError, ValueError):
            continue
        transfer_availability_ids.add(availability_id)
        transfer_expense_ids.add(row["expense"].pk)
        slot_key = str(meta.get("court_refund_slot_key") or "").strip()
        if slot_key:
            transfer_slot_keys.add(slot_key)

    # 新方式の登録と同じ開催回を示す旧方式データは、移行前の重複記録として扱う。
    # 給与計算は availability_id を持つ新方式だけを正とし、旧データを
    # 「レッスンと不一致」の警告へ重複加算しない。
    superseded_legacy_expense_ids = {
        row["expense"].pk
        for row in expenses
        if row["is_court"]
        and row["expense"].pk not in transfer_expense_ids
        and row["slot_key"]
        and row["slot_key"] in transfer_slot_keys
    }

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
    used_expense_ids.update(superseded_legacy_expense_ids)

    for reservation in reservations:
        # 新方式の登録は _court_transfer_allocation で既に給与へ反映済み。
        # 同じ開催回を旧方式でも再照合すると「未登録」が二重判定される。
        if getattr(reservation, "availability_id", None) in transfer_availability_ids:
            continue

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
                "date": _local_datetime(reservation.start_at).date().isoformat(),
                "start_at": reservation.start_at.isoformat(),
                "end_at": reservation.end_at.isoformat(),
                "court": str(getattr(reservation, "court", "") or ""),
                "court_count": max(
                    int(getattr(reservation, "court_count", 1) or 1), 1
                ),
                "slot_key": slot_key,
                "calculated_cost": expected_cost,
                "registered_cost": finalized_cost if is_finalized else None,
                "canonical_cost": finalized_cost if is_finalized else expected_cost,
                "expected_cost": expected_cost,
                "finalized_cost": finalized_cost,
                "payer_id": payer_id,
                "using_coach_ids": [coach.pk for coach in coaches],
                "burden_target_ids": burden_targets,
                "burden_rule": burden_rule,
                "is_finalized": is_finalized,
                "execution_status": "held",
                "canceled": False,
                "rain_canceled": False,
                "included_reason": (
                    "held occurrence with finalized registered court cost"
                    if is_finalized
                    else "held occurrence; expected cost excluded until registered"
                ),
            }
        )

    unused_registered_total = 0
    for row in expenses:
        if not row["is_court"]:
            continue
        if row["expense"].pk in used_expense_ids:
            continue
        # 開催回キーを持たない旧方式のコート代は、現在のレッスンへ安全に
        # 照合できない移行前データである。availability_id 付きの新方式を
        # 正規記録とし、この旧データだけを不一致警告へ加算しない。
        if (
            row["meta"].get("record_kind") != COURT_TRANSFER_RECORD_KIND
            and not row["slot_key"]
        ):
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


def _build_other_expense_policy(
    year,
    month,
    main_coach_ids,
    participant_count_by_coach=None,
):
    month_start, next_month = _month_range(year, month)
    expenses = _approved_monthly_expenses(month_start, next_month)

    burden_by_coach = defaultdict(int)
    ball_burden_by_coach = defaultdict(int)
    other_burden_by_coach = defaultdict(int)
    ball_reimbursement_by_coach = defaultdict(int)
    other_reimbursement_by_coach = defaultdict(int)
    reimbursement_by_coach = defaultdict(int)
    detail_rows = []

    for row in expenses:
        if row["is_court"]:
            continue
        if row["expense_type"] == EXPENSE_TYPE_PERSONAL:
            # 個人事業主が自身の経費管理のために記録する項目。
            # 会社の給与・負担・返金には一切含めない。
            continue

        amount = row["amount"]
        payer_id = row["payer_id"]
        target_ids = list(main_coach_ids)
        is_ball_expense = getattr(row["expense"], "category", "") == "ball"
        if is_ball_expense:
            allocations = _split_amount_by_lesson_count(
                amount,
                target_ids,
                participant_count_by_coach,
            )
            rule = "完了済みレッスンの担当参加人数に比例"
        else:
            allocations = _split_amount(amount, target_ids)
            rule = "メインコーチ3人均等負担"

        if payer_id:
            if is_ball_expense:
                # 立替者自身の按分額は本人負担として残し、他コーチから
                # 控除する分だけを立替者への付与として計上する。
                reimbursement_amount = max(
                    amount - _money(allocations.get(payer_id)),
                    0,
                )
                ball_reimbursement_by_coach[payer_id] += reimbursement_amount
            else:
                reimbursement_amount = amount
                other_reimbursement_by_coach[payer_id] += reimbursement_amount
            reimbursement_by_coach[payer_id] += reimbursement_amount

        for coach_id, allocated in allocations.items():
            burden_by_coach[coach_id] += allocated
            if is_ball_expense:
                ball_burden_by_coach[coach_id] += allocated
            else:
                other_burden_by_coach[coach_id] += allocated

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
        "ball_burden_by_coach": dict(ball_burden_by_coach),
        "other_burden_by_coach": dict(other_burden_by_coach),
        "ball_reimbursement_by_coach": dict(ball_reimbursement_by_coach),
        "other_reimbursement_by_coach": dict(other_reimbursement_by_coach),
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


def _active_reimbursement_payment_total(settlement, coach):
    """旧方式で登録済みの立替精算も、最終受取額の支払済みとして扱う。"""
    from .settlement_models import SettlementPayment

    result = SettlementPayment.objects.filter(
        monthly_settlement=settlement,
        coach=coach,
        payment_type=SettlementPayment.PAYMENT_TYPE_REIMBURSEMENT,
        is_reversed=False,
    ).aggregate(total=Sum("amount"))
    return _money(result.get("total"))


def _negative_carry_in_by_coach(year, month, coach_ids):
    """直前の締め済み月からコーチ別のマイナス残高を引き継ぐ。"""
    from .settlement_models import CoachMonthlySettlement, MonthlySettlement

    if month == 1:
        previous_year, previous_month = year - 1, 12
    else:
        previous_year, previous_month = year, month - 1

    previous_rows = CoachMonthlySettlement.objects.filter(
        monthly_settlement__year=previous_year,
        monthly_settlement__month=previous_month,
        monthly_settlement__status=MonthlySettlement.STATUS_CLOSED,
        coach_id__in=coach_ids,
    ).values("coach_id", "calculation_snapshot")

    carry_by_coach = {}
    for previous_row in previous_rows:
        snapshot = dict(previous_row.get("calculation_snapshot") or {})
        negative_carry = max(
            _money(snapshot.get("negative_carry")),
            0,
        )
        if negative_carry:
            carry_by_coach[previous_row["coach_id"]] = negative_carry
    return carry_by_coach


def _unpaid_salary_carry_in_by_coach(year, month, coach_ids):
    """直前の締め済み月からコーチ別の給与未払い残高を引き継ぐ。"""
    from .settlement_models import CoachMonthlySettlement, MonthlySettlement

    if month == 1:
        previous_year, previous_month = year - 1, 12
    else:
        previous_year, previous_month = year, month - 1

    previous_rows = CoachMonthlySettlement.objects.filter(
        monthly_settlement__year=previous_year,
        monthly_settlement__month=previous_month,
        monthly_settlement__status=MonthlySettlement.STATUS_CLOSED,
        coach_id__in=coach_ids,
    ).values("coach_id", "salary_unpaid")

    carry_by_coach = {}
    for previous_row in previous_rows:
        unpaid_salary = max(
            _money(previous_row.get("salary_unpaid")),
            0,
        )
        if unpaid_salary:
            carry_by_coach[previous_row["coach_id"]] = unpaid_salary
    return carry_by_coach


def _apply_wallet_policy(result, year, month):
    settlement = result.get("settlement")
    if settlement is None or settlement.is_closed:
        return result

    coach_rows = list(result.get("coach_rows") or [])
    if not coach_rows:
        return result

    main_coach_list = main_coaches()
    main_coach_ids = [coach.pk for coach in main_coach_list]
    eligible_coach_ids = [
        getattr(row.get("coach"), "pk", None)
        for row in coach_rows
        if getattr(row.get("coach"), "pk", None) is not None
    ]
    negative_carry_in_by_coach = _negative_carry_in_by_coach(
        year,
        month,
        eligible_coach_ids,
    )
    unpaid_salary_carry_in_by_coach = _unpaid_salary_carry_in_by_coach(
        year,
        month,
        eligible_coach_ids,
    )

    expense_policies = build_expense_distribution_policies(
        year=year,
        month=month,
        main_coach_ids=main_coach_ids,
        eligible_coach_ids=eligible_coach_ids,
        contractor_coach_ids=[
            getattr(row.get("coach"), "pk", None)
            for row in coach_rows
            if row.get("is_contractor_coach")
            and getattr(row.get("coach"), "pk", None) is not None
        ],
        build_court_cost_policy=_build_court_cost_policy,
        build_other_expense_policy=_build_other_expense_policy,
        held_participant_count_by_coach=_held_participant_count_by_coach,
        build_rain_refund_policy=_rain_refund_policy,
    )
    court_policy = expense_policies["court_policy"]
    other_expense_policy = expense_policies["other_expense_policy"]
    rain_refund_policy = expense_policies["rain_refund_policy"]

    contractor_pay_total = sum(
        _money(row.get("contractor_hourly_pay_amount"))
        for row in coach_rows
        if row.get("is_contractor_coach")
    )
    contractor_share_by_main = _split_amount(
        contractor_pay_total,
        main_coach_ids,
    )

    ticket_purchase_cash = _money(result.get("ticket_purchase_total"))
    total_company_revenue = _company_cash_in_total(result, coach_rows)

    coach_calculation = calculate_coach_wallets(
        coach_rows=coach_rows,
        settlement=settlement,
        main_coach_ids=main_coach_ids,
        court_policy=court_policy,
        other_expense_policy=other_expense_policy,
        rain_refund_policy=rain_refund_policy,
        contractor_share_by_main=contractor_share_by_main,
        negative_carry_in_by_coach=negative_carry_in_by_coach,
        unpaid_salary_carry_in_by_coach=unpaid_salary_carry_in_by_coach,
        total_company_revenue=total_company_revenue,
        money=_money,
        active_salary_payment_total=_active_salary_payment_total,
        active_reimbursement_payment_total=(
            _active_reimbursement_payment_total
        ),
    )
    coach_rows = coach_calculation["coach_rows"]
    wallet_difference = coach_calculation["wallet_difference"]
    salary_due_total = coach_calculation["salary_due_total"]
    salary_paid_total = coach_calculation["salary_paid_total"]
    reimbursement_paid_total = coach_calculation[
        "reimbursement_paid_total"
    ]
    unpaid_salary_total = coach_calculation["unpaid_salary_total"]
    negative_carry_total = coach_calculation["negative_carry_total"]
    adjustment_by_coach = {}
    company_internal_reserve = max(
        _money(settlement.opening_balance),
        0,
    )
    settlement.opening_balance = company_internal_reserve
    settlement.cash_in_total = total_company_revenue
    settlement.ticket_cash_in = ticket_purchase_cash
    settlement.preopen_cash_in = _money(
        result.get("preopen_paid_total")
    )
    settlement.stringing_cash_in = _money(
        result.get("stringing_total")
    )
    settlement.salary_cash_out = salary_paid_total
    settlement.reimbursement_cash_out = reimbursement_paid_total
    settlement.common_expense_cash_out = 0
    settlement.contractor_cash_out = contractor_pay_total
    settlement.cash_out_total = salary_paid_total + reimbursement_paid_total
    settlement.unpaid_salary_total = unpaid_salary_total
    settlement.unpaid_reimbursement_total = 0
    settlement.closing_balance = max(
        company_internal_reserve
        + total_company_revenue
        - salary_paid_total
        - reimbursement_paid_total,
        0,
    )

    settlement_snapshot = dict(settlement.calculation_snapshot or {})
    settlement_snapshot.update(
        {
            "wallet_policy": True,
            "company_internal_reserve": company_internal_reserve,
            "company_revenue_definition": (
                "ticket_purchase_cash + collected_cash + stringing"
            ),
            "ticket_consumption_revenue": _money(
                result.get("ticket_amount_total")
            ),
            "main_coach_names": list(MAIN_COACH_NAMES),
            "main_coach_ids": main_coach_ids,
            "total_company_revenue": total_company_revenue,
            "contractor_pay_total": contractor_pay_total,
            "contractor_share_by_main": contractor_share_by_main,
            "court_policy": court_policy,
            "other_expense_policy": other_expense_policy,
            "rain_refund_policy": rain_refund_policy,
            "wallet_difference_before_adjustment": wallet_difference,
            "wallet_adjustment_by_coach": adjustment_by_coach,
            "negative_carry_total": negative_carry_total,
            "rain_refund_pending_rows": rain_refund_policy["pending_rows"],
            "rain_refund_pending_total": rain_refund_policy["pending_total"],
            "rain_refunded_rows": rain_refund_policy["refunded_rows"],
            "rain_refunded_total": rain_refund_policy["refunded_total"],
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
            "opening_balance": company_internal_reserve,
            "salary_due_total": salary_due_total,
            "salary_paid_total": salary_paid_total,
            "unpaid_salary_total": unpaid_salary_total,
            "reimbursement_due_total": 0,
            "reimbursement_paid_total": reimbursement_paid_total,
            "unpaid_reimbursement_total": 0,
            "cash_out_total": salary_paid_total + reimbursement_paid_total,
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
            "rain_refund_pending_rows": rain_refund_policy["pending_rows"],
            "rain_refund_pending_total": rain_refund_policy["pending_total"],
            "rain_refunded_rows": rain_refund_policy["refunded_rows"],
            "rain_refunded_total": rain_refund_policy["refunded_total"],
        }
    )
    return result
