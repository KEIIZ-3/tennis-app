from collections import defaultdict

from django.utils import timezone

from .expense_metadata import parse_expense_note
from .models import (
    CoachAvailability,
    CoachExpense,
    Reservation,
)
from .lesson_participants import CAPACITY_CONSUMING_STATUSES
from .lesson_occurrence_resolver import resolve_authoritative_fixed_lesson
from .court_cost_allocation import allocate_court_cost


COURT_TRANSFER_RECORD_KIND = "court_transfer"


def _money(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _split_amount(amount, coach_ids):
    unique_ids = list(dict.fromkeys(coach_id for coach_id in coach_ids if coach_id))
    if not unique_ids:
        return {}
    total = max(_money(amount), 0)
    base, remainder = divmod(total, len(unique_ids))
    return {
        coach_id: base + (1 if index < remainder else 0)
        for index, coach_id in enumerate(unique_ids)
    }


def _coach_ids_from_fixed_lesson(fixed_lesson, eligible_id_set):
    result = []
    try:
        coaches = fixed_lesson.all_coaches()
    except Exception:
        coaches = (
            getattr(fixed_lesson, "coach", None),
            getattr(fixed_lesson, "coach_2", None),
            getattr(fixed_lesson, "coach_3", None),
        )

    for coach in coaches:
        coach_id = getattr(coach, "pk", None)
        if coach_id in eligible_id_set and coach_id not in result:
            result.append(coach_id)
    return result


def _saved_using_coach_ids(meta, eligible_id_set):
    result = []
    for value in meta.get("using_coach_ids") or []:
        coach_id = _money(value)
        if coach_id in eligible_id_set and coach_id not in result:
            result.append(coach_id)
    return result


def _local_datetime(value):
    if value is None:
        return None
    try:
        if timezone.is_aware(value):
            return timezone.localtime(value)
    except Exception:
        pass
    return value


def _scheduled_fixed_lesson_match(availability, eligible_id_set):
    """明示リレーションまたは一意なlegacy証拠から開催回担当を特定する。"""
    if availability is None:
        return [], []
    fixed_lesson = resolve_authoritative_fixed_lesson(availability)
    if fixed_lesson is None:
        return [], []
    return (
        _coach_ids_from_fixed_lesson(fixed_lesson, eligible_id_set),
        [fixed_lesson.pk],
    )


def _fixed_occurrence_coach_ids(availability, eligible_id_set):
    """予約に固定レッスンが保存されている場合の担当コーチを返す。"""
    if availability is None:
        return []

    reservations = (
        Reservation.objects.filter(
            availability=availability,
            start_at=availability.start_at,
            end_at=availability.end_at,
            status__in=CAPACITY_CONSUMING_STATUSES,
            fixed_lesson_id__isnull=False,
        )
        .select_related(
            "fixed_lesson",
            "fixed_lesson__coach",
            "fixed_lesson__coach_2",
            "fixed_lesson__coach_3",
        )
        .order_by("id")
    )

    result = []
    seen_fixed_lesson_ids = set()
    for reservation in reservations:
        fixed_lesson = getattr(reservation, "fixed_lesson", None)
        fixed_lesson_id = getattr(reservation, "fixed_lesson_id", None)
        if fixed_lesson is None or fixed_lesson_id in seen_fixed_lesson_ids:
            continue
        seen_fixed_lesson_ids.add(fixed_lesson_id)

        for coach_id in _coach_ids_from_fixed_lesson(
            fixed_lesson,
            eligible_id_set,
        ):
            if coach_id not in result:
                result.append(coach_id)

    return result


def _reservation_coach_ids(availability, eligible_id_set):
    if availability is None:
        return []

    result = []
    reservations = (
        Reservation.objects.filter(
            availability=availability,
            start_at=availability.start_at,
            end_at=availability.end_at,
            status__in=CAPACITY_CONSUMING_STATUSES,
        )
        .select_related("coach", "substitute_coach")
        .order_by("id")
    )
    for reservation in reservations:
        coach_id = reservation.substitute_coach_id or reservation.coach_id
        if coach_id in eligible_id_set and coach_id not in result:
            result.append(coach_id)
    return result


def _availability_coach_ids(availability, eligible_id_set):
    if availability is None:
        return []

    coach_id = availability.substitute_coach_id or availability.coach_id
    return [coach_id] if coach_id in eligible_id_set else []


def _transfer_slot_key(meta, row):
    return str(
        meta.get("court_refund_slot_key")
        or row.get("slot_key")
        or ""
    ).strip()


def reconcile_court_policy(
    court_policy,
    *,
    main_coach_ids,
    eligible_coach_ids,
    contractor_coach_ids,
):
    """
    開催回の明示リレーションを最優先にコート代を再配賦する。

    予約または開催枠に固定レッスンの正本が保存されている場合は現在の担当設定を使う。
    独立開催枠は、同じ日時・コートに別の固定レッスンがあっても混同しない。
    """
    policy = dict(court_policy or {})
    detail_rows = [dict(row) for row in policy.get("detail_rows") or []]
    transfer_rows = [row for row in detail_rows if row.get("is_court_transfer")]
    if not transfer_rows:
        return policy

    expense_ids = [
        _money(row.get("expense_id"))
        for row in transfer_rows
        if _money(row.get("expense_id")) > 0
    ]
    expense_map = {
        expense.pk: expense
        for expense in CoachExpense.objects.filter(pk__in=expense_ids)
    }

    transfer_meta = {}
    availability_ids = set()
    canonical_expense_by_slot = {}

    for row in transfer_rows:
        expense_id = _money(row.get("expense_id"))
        expense = expense_map.get(expense_id)
        meta = parse_expense_note(expense.note) if expense else {}
        if meta.get("record_kind") != COURT_TRANSFER_RECORD_KIND:
            continue

        try:
            availability_id = int(meta.get("availability_id"))
        except (TypeError, ValueError):
            availability_id = None

        slot_key = _transfer_slot_key(meta, row)
        transfer_meta[expense_id] = {
            "availability_id": availability_id,
            "slot_key": slot_key,
            "meta": meta,
        }
        if availability_id:
            availability_ids.add(availability_id)
        if slot_key:
            current = canonical_expense_by_slot.get(slot_key)
            if current is None or expense_id > current:
                canonical_expense_by_slot[slot_key] = expense_id

    availability_map = {
        availability.pk: availability
        for availability in CoachAvailability.objects.filter(
            pk__in=availability_ids
        ).select_related(
            "coach", "substitute_coach", "court",
            "fixed_lesson_source", "fixed_lesson_source__coach",
            "fixed_lesson_source__coach_2", "fixed_lesson_source__coach_3",
            "fixed_lesson_source__court",
        )
    }

    eligible_id_set = set(eligible_coach_ids or [])
    main_ids = [coach_id for coach_id in main_coach_ids if coach_id in eligible_id_set]
    contractor_id_set = set(contractor_coach_ids or [])
    canonical_transfer_slot_keys = set(canonical_expense_by_slot)

    burden_by_coach = defaultdict(int)
    reimbursement_by_coach = defaultdict(int)
    reconciled_rows = []

    for row in detail_rows:
        if not row.get("is_court_transfer"):
            slot_key = str(row.get("slot_key") or "").strip()
            if slot_key and slot_key in canonical_transfer_slot_keys:
                continue

            finalized_cost = _money(row.get("finalized_cost"))
            target_ids = [
                _money(value)
                for value in row.get("burden_target_ids") or []
                if _money(value) in eligible_id_set
            ]
            for coach_id, amount in _split_amount(
                finalized_cost,
                target_ids,
            ).items():
                burden_by_coach[coach_id] += amount

            payer_id = _money(row.get("payer_id"))
            if payer_id in eligible_id_set and finalized_cost:
                reimbursement_by_coach[payer_id] += finalized_cost
            reconciled_rows.append(row)
            continue

        expense_id = _money(row.get("expense_id"))
        transfer = transfer_meta.get(expense_id)
        if transfer is None:
            continue

        slot_key = transfer["slot_key"]
        if slot_key and canonical_expense_by_slot.get(slot_key) != expense_id:
            continue

        meta = transfer["meta"]
        availability_id = transfer["availability_id"]
        availability = availability_map.get(availability_id)

        scheduled_ids, scheduled_fixed_lesson_ids = _scheduled_fixed_lesson_match(
            availability,
            eligible_id_set,
        )
        fixed_occurrence_ids = _fixed_occurrence_coach_ids(
            availability,
            eligible_id_set,
        )
        availability_ids_for_row = _availability_coach_ids(
            availability,
            eligible_id_set,
        )
        reservation_ids = _reservation_coach_ids(
            availability,
            eligible_id_set,
        )
        saved_ids = _saved_using_coach_ids(meta, eligible_id_set)

        target_ids = scheduled_ids
        source_label = "開催回の正本固定レッスン担当"

        if not target_ids:
            target_ids = availability_ids_for_row
            source_label = "開催枠の担当履歴"

        if not target_ids:
            target_ids = reservation_ids
            source_label = "開催回予約の担当履歴"

        if not target_ids:
            target_ids = saved_ids
            source_label = "コート代登録時の担当履歴"

        amount = max(_money(row.get("amount")), 0)
        allocation = allocate_court_cost(
            amount,
            target_ids,
            main_coach_ids=main_ids,
            contractor_coach_ids=contractor_id_set,
        )
        burden_target_ids = allocation["burden_target_ids"]

        for coach_id, allocated in allocation["burden_by_coach"].items():
            burden_by_coach[coach_id] += allocated

        payer_id = _money(meta.get("payer_coach_id") or row.get("payer_id"))
        if payer_id in eligible_id_set:
            reimbursement_by_coach[payer_id] += amount

        reconciled_rows.append(
            {
                **row,
                "availability_id": availability_id,
                "slot_key": slot_key,
                "burden_target_ids": burden_target_ids,
                "payer_id": payer_id,
                "burden_rule": (
                    "業務委託コーチのみのためメインコーチ3人で均等負担"
                    if allocation["rule"] == "contractor_only"
                    else f"{source_label}で負担"
                ),
                "lesson_label": meta.get("court_refund_lesson_label") or "",
                "reconciliation_source": source_label,
                "scheduled_fixed_lesson_ids": scheduled_fixed_lesson_ids,
                "scheduled_fixed_lesson_coach_ids": scheduled_ids,
                "reservation_fixed_lesson_coach_ids": fixed_occurrence_ids,
                "availability_coach_ids": availability_ids_for_row,
                "reservation_coach_ids": reservation_ids,
                "saved_using_coach_ids": saved_ids,
            }
        )

    policy.update(
        {
            "burden_by_coach": dict(burden_by_coach),
            "reimbursement_by_coach": dict(reimbursement_by_coach),
            "detail_rows": reconciled_rows,
            "finalized_court_cost_total": sum(burden_by_coach.values()),
            "court_reimbursement_total": sum(reimbursement_by_coach.values()),
        }
    )
    return policy
