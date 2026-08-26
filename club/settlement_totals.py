def calculate_settlement_totals(
    *,
    coach_rows,
    ticket_cash_receipts,
    stringing_total,
    approved_common_expense_total,
    submitted_personal_expense_rows,
    expense_approval_submitted,
    money,
):
    preopen_paid_total = sum(row["preopen_paid_amount"] for row in coach_rows)
    preopen_unpaid_total = sum(row["preopen_unpaid_amount"] for row in coach_rows)
    ticket_amount_total = sum(row["ticket_amount"] for row in coach_rows)

    ticket_purchase_total = sum(money(receipt.amount) for receipt in ticket_cash_receipts)

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

    pending_personal_reimbursement_total = sum(
        money(row["expense"].amount)
        for row in submitted_personal_expense_rows
        if row["approval_status"] == expense_approval_submitted
    )

    cash_in_total = (
        preopen_paid_total + ticket_purchase_total + stringing_total
    )
    cash_out_total = (
        salary_paid_total
        + reimbursement_paid_total
        + approved_common_expense_total
    )

    return {
        "preopen_paid_total": preopen_paid_total,
        "preopen_unpaid_total": preopen_unpaid_total,
        "ticket_amount_total": ticket_amount_total,
        "ticket_purchase_total": ticket_purchase_total,
        "salary_due_total": salary_due_total,
        "reimbursement_due_total": reimbursement_due_total,
        "salary_paid_total": salary_paid_total,
        "reimbursement_paid_total": reimbursement_paid_total,
        "unpaid_salary_total": unpaid_salary_total,
        "unpaid_reimbursement_total": unpaid_reimbursement_total,
        "pending_personal_reimbursement_total": (
            pending_personal_reimbursement_total
        ),
        "cash_in_total": cash_in_total,
        "cash_out_total": cash_out_total,
    }
