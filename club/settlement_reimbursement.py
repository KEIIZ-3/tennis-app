def apply_reimbursement_amounts(
    *,
    approved_personal_expense_rows,
    coach_map,
    month_start,
    next_month,
    expense_unpaid_amount,
):
    through_date = next_month - __import__("datetime").timedelta(days=1)

    for row in approved_personal_expense_rows:
        expense = row["expense"]
        coach_id = getattr(expense, "created_by_id", None)
        if coach_id not in coach_map:
            continue

        unpaid = expense_unpaid_amount(
            expense,
            through_date=through_date,
        )
        if expense.expense_date < month_start:
            coach_map[coach_id]["reimbursement_carry_in"] += unpaid
        else:
            coach_map[coach_id]["reimbursement_current_month"] += unpaid
