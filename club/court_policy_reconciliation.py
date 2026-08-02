from collections import defaultdict

from .expense_metadata import parse_expense_note
from .models import CoachAvailability, CoachExpense, Reservation


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


def _saved_using_coach_ids(meta, eligible_id_set):
    result = []
    for value in meta.get("using_coach_ids") or []:
        coach_id = _money(value)
        if coach_id in eligible_id_set and coach_id not in result:
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
            status__in=(Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING),
        )
        .select_related("coach", "substitute_coach")
        .order_by("id")
    )
    for reservation in reservations:
        coach_id = reservation.substitute_coach_id or reservation.coach_id
        if coach_id in eligible_id_set and coach_id not in result:
            result.append(coach_id)
    return result


def _fallback_using_coach_ids(availability, eligible_id_set):
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
    開催回の予約レコードに保存された担当者を最優先で再配賦する。

    コート代登録時の using_coach_ids は、担当変更前の情報を保持している場合が
    あるため、対象開催回の有効予約から担当者を取得できない場合だけ使用する。
    現在の固定レッスン設定は過去開催分へ遡及適用しない。
    同じ開催回に新旧のコート代記録が重複する場合は、開催回キーごとに
    最新の正規コート代登録だけを採用する。
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
        ).select_related("coach", "substitute_coach", "court")
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

        target_ids = _reservation_coach_ids(
            availability,
            eligible_id_set,
        )
        source_label = "開催回予約の担当履歴"

        if not target_ids:
            target_ids = _saved_using_coach_ids(meta, eligible_id_set)
            source_label = "コート代登録時の担当履歴"

        if not target_ids:
            target_ids = _fallback_using_coach_ids(
                availability,
                eligible_id_set,
            )
            source_label = "開催枠の担当履歴"

        contractor_only = bool(target_ids) and all(
            coach_id in contractor_id_set for coach_id in target_ids
        )
        burden_target_ids = main_ids if contractor_only else target_ids
        amount = max(_money(row.get("amount")), 0)

        for coach_id, allocated in _split_amount(
            amount,
            burden_target_ids,
        ).items():
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
                    if contractor_only
                    else f"{source_label}で負担"
                ),
                "lesson_label": meta.get("court_refund_lesson_label") or "",
                "reconciled_from_reservation_history": bool(
                    _reservation_coach_ids(availability, eligible_id_set)
                ),
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
