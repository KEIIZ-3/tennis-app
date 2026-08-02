from collections import defaultdict

from django.db.models import Q

from .expense_metadata import parse_expense_note
from .models import CoachAvailability, CoachExpense, FixedLesson, Reservation


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


def _fixed_lesson_coach_ids(availability):
    target_date = availability.start_at.date()
    lessons = (
        FixedLesson.objects.filter(
            is_active=True,
            weekday=target_date.weekday(),
            start_hour=availability.start_at.hour,
            lesson_type=availability.lesson_type,
        )
        .filter(Q(court=availability.court) | Q(court__isnull=True))
        .select_related("coach", "coach_2", "coach_3")
        .order_by("id")
    )

    candidates = []
    for lesson in lessons:
        try:
            occurrence_dates = set(lesson.scheduled_occurrence_dates())
        except Exception:
            occurrence_dates = set()
        if occurrence_dates and target_date not in occurrence_dates:
            continue

        try:
            coaches = lesson.all_coaches()
        except Exception:
            coaches = [lesson.coach, lesson.coach_2, lesson.coach_3]

        coach_ids = []
        for coach in coaches:
            coach_id = getattr(coach, "pk", None)
            if coach_id and coach_id not in coach_ids:
                coach_ids.append(coach_id)
        if coach_ids:
            candidates.append((lesson.pk, coach_ids))

    if not candidates:
        return []

    reservation_fixed_ids = set(
        Reservation.objects.filter(
            availability=availability,
            start_at=availability.start_at,
            end_at=availability.end_at,
            status__in=(Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING),
            fixed_lesson_id__isnull=False,
        ).values_list("fixed_lesson_id", flat=True)
    )
    for lesson_id, coach_ids in candidates:
        if lesson_id in reservation_fixed_ids:
            return coach_ids

    return candidates[0][1]


def _authoritative_using_coach_ids(availability):
    if availability.substitute_coach_id:
        return [availability.substitute_coach_id]

    fixed_ids = _fixed_lesson_coach_ids(availability)
    if fixed_ids:
        return fixed_ids

    reservation_ids = []
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
        if coach_id and coach_id not in reservation_ids:
            reservation_ids.append(coach_id)
    if reservation_ids:
        return reservation_ids

    return [availability.coach_id] if availability.coach_id else []


def reconcile_court_policy(
    court_policy,
    *,
    main_coach_ids,
    eligible_coach_ids,
    contractor_coach_ids,
):
    """登録時メタデータではなく、現在の開催枠・固定レッスン設定を正として再配賦する。"""
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

    availability_ids = set()
    transfer_meta = {}
    for row in transfer_rows:
        expense_id = _money(row.get("expense_id"))
        expense = expense_map.get(expense_id)
        meta = parse_expense_note(expense.note) if expense else {}
        if meta.get("record_kind") != COURT_TRANSFER_RECORD_KIND:
            continue
        try:
            availability_id = int(meta.get("availability_id"))
        except (TypeError, ValueError):
            continue
        availability_ids.add(availability_id)
        transfer_meta[expense_id] = (availability_id, meta)

    availability_map = {
        availability.pk: availability
        for availability in CoachAvailability.objects.filter(
            pk__in=availability_ids
        ).select_related("coach", "substitute_coach", "court")
    }

    eligible_id_set = set(eligible_coach_ids or [])
    main_ids = [coach_id for coach_id in main_coach_ids if coach_id in eligible_id_set]
    contractor_id_set = set(contractor_coach_ids or [])

    burden_by_coach = defaultdict(int)
    reimbursement_by_coach = defaultdict(int)
    reconciled_rows = []

    for row in detail_rows:
        if not row.get("is_court_transfer"):
            for coach_id, amount in _split_amount(
                _money(row.get("finalized_cost")),
                row.get("burden_target_ids") or [],
            ).items():
                burden_by_coach[coach_id] += amount
            payer_id = _money(row.get("payer_id"))
            finalized_cost = _money(row.get("finalized_cost"))
            if payer_id and finalized_cost:
                reimbursement_by_coach[payer_id] += finalized_cost
            reconciled_rows.append(row)
            continue

        expense_id = _money(row.get("expense_id"))
        availability_id, meta = transfer_meta.get(expense_id, (None, {}))
        availability = availability_map.get(availability_id)
        if availability is None:
            target_ids = [
                _money(value)
                for value in row.get("burden_target_ids") or []
                if _money(value) in eligible_id_set
            ]
        else:
            target_ids = [
                coach_id
                for coach_id in _authoritative_using_coach_ids(availability)
                if coach_id in eligible_id_set
            ]

        contractor_only = bool(target_ids) and all(
            coach_id in contractor_id_set for coach_id in target_ids
        )
        burden_target_ids = main_ids if contractor_only else target_ids
        amount = max(_money(row.get("amount")), 0)
        for coach_id, allocated in _split_amount(amount, burden_target_ids).items():
            burden_by_coach[coach_id] += allocated

        payer_id = _money(meta.get("payer_coach_id") or row.get("payer_id"))
        if payer_id in eligible_id_set:
            reimbursement_by_coach[payer_id] += amount

        reconciled_rows.append(
            {
                **row,
                "availability_id": availability_id,
                "burden_target_ids": burden_target_ids,
                "payer_id": payer_id,
                "burden_rule": (
                    "業務委託コーチのみのためメインコーチ3人で均等負担"
                    if contractor_only
                    else "現在の開催枠担当コーチで均等負担"
                ),
                "lesson_label": meta.get("court_refund_lesson_label") or "",
                "reconciled_from_current_schedule": bool(availability),
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
