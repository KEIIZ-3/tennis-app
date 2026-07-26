def build_coach_map(coaches, *, display_name, money):
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
    return coach_map
