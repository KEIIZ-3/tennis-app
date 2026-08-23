def reservation_coaches_for_split(reservation):
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
                and getattr(coach, "role", "")
                in ("coach", "contractor_coach")
            ]
            if coaches:
                return coaches
        except Exception:
            pass

    assigned = None
    availability = getattr(reservation, "availability", None)
    if availability and hasattr(availability, "all_coaches"):
        coaches = [
            coach for coach in availability.all_coaches()
            if getattr(coach, "role", "") in ("coach", "contractor_coach")
        ]
        if coaches:
            return coaches

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


def reservation_duration_minutes(reservation):
    try:
        return max(
            int(
                (reservation.end_at - reservation.start_at).total_seconds()
                // 60
            ),
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


from .stringing_service import stringing_revenue_amount
from .lesson_execution_storage import is_held_finished_reservation


def aggregate_reservations(
    *,
    reservations,
    coach_map,
    reservation_model,
    preopen_cash_price,
    is_preopen_cash_lesson_date,
    money,
    execution_status_map,
):
    active_regular_coach_ids = set()
    active_coach_ids = set()

    for reservation in reservations:
        split_coaches = reservation_coaches_for_split(reservation)
        if not split_coaches:
            continue

        denominator = max(len(split_coaches), 1)
        snapshot = getattr(reservation, "participant_ticket_price_snapshot", None)
        ticket_total = money(snapshot) if snapshot is not None else sum(
            money(consumption.unit_price_snapshot) * money(consumption.tickets_used)
            for consumption in reservation.ticket_consumptions.filter(refunded_at__isnull=True)
        )
        payment_amount = money(
            getattr(reservation, "payment_amount", 0) or preopen_cash_price
        )
        is_preopen = (
            reservation.lesson_type == reservation_model.LESSON_GENERAL
            and is_preopen_cash_lesson_date(reservation.start_at)
            and reservation.is_payment_tracking_required()
        )
        if not is_preopen and not is_held_finished_reservation(
            reservation,
            execution_status_map,
        ):
            continue

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
                    row["contractor_work_minutes"] += (
                        reservation_duration_minutes(reservation)
                    )

            if ticket_total > 0:
                row["ticket_amount"] += int(ticket_total / denominator)

            if is_preopen:
                split_amount = int(payment_amount / denominator)
                if (
                    reservation.payment_status
                    == reservation_model.PAYMENT_STATUS_PAID
                ):
                    row["preopen_paid_amount"] += split_amount
                elif (
                    reservation.payment_status
                    == reservation_model.PAYMENT_STATUS_WAIVED
                ):
                    row["preopen_waived_amount"] += split_amount
                else:
                    row["preopen_unpaid_amount"] += split_amount

    return active_regular_coach_ids, active_coach_ids


def aggregate_stringing_orders(*, stringing_orders, coach_map, money):
    stringing_total = 0

    for order in stringing_orders:
        amount = money(stringing_revenue_amount(order))
        if amount <= 0:
            continue
        stringing_total += amount

        assigned_coach_id = getattr(order, "assigned_coach_id", None)
        if assigned_coach_id in coach_map:
            coach_map[assigned_coach_id]["stringing_amount"] += amount

    return stringing_total


def classify_expense_rows(
    *,
    all_expense_meta_rows,
    month_start,
    next_month,
    expense_type_common,
    expense_type_personal,
    approval_approved,
    approval_submitted,
):
    monthly_expense_meta_rows = [
        row
        for row in all_expense_meta_rows
        if month_start <= row["expense"].expense_date < next_month
    ]
    approved_common_expense_rows = [
        row
        for row in monthly_expense_meta_rows
        if not row["is_payout"]
        and row["approval_status"] == approval_approved
        and row["expense_type"] == expense_type_common
    ]
    approved_personal_expense_rows = [
        row
        for row in all_expense_meta_rows
        if not row["is_payout"]
        and row["approval_status"] == approval_approved
        and row["expense_type"] == expense_type_personal
    ]
    submitted_personal_expense_rows = [
        row
        for row in all_expense_meta_rows
        if not row["is_payout"]
        and row["expense_type"] == expense_type_personal
        and row["approval_status"]
        in (approval_submitted, approval_approved)
    ]

    return {
        "monthly_expense_meta_rows": monthly_expense_meta_rows,
        "approved_common_expense_rows": approved_common_expense_rows,
        "approved_personal_expense_rows": approved_personal_expense_rows,
        "submitted_personal_expense_rows": submitted_personal_expense_rows,
    }


def calculate_coach_rows(
    *,
    coach_map,
    active_regular_coach_ids,
    approved_common_expense_total,
    settlement,
    current_payment_totals,
):
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
        salary_paid, reimbursement_paid = current_payment_totals(
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

    return {
        "coach_rows": coach_rows,
        "contractor_hourly_pay_total": contractor_hourly_pay_total,
        "common_expense_base_total": common_expense_base_total,
        "common_expense_participant_count": common_expense_participant_count,
        "per_coach_common_expense": per_coach_common_expense,
    }
