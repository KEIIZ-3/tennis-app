def _money(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def build_monthly_profit_rows(coach_rows):
    """Build display-only operating profit from official settlement amounts."""
    profit_rows = []
    for row in coach_rows:
        if not row.get("is_main_coach"):
            continue

        ticket_revenue = _money(row.get("ticket_amount"))
        cash_revenue = _money(row.get("preopen_paid_amount"))
        stringing_revenue = _money(row.get("stringing_amount"))
        revenue_total = ticket_revenue + cash_revenue + stringing_revenue

        court_cost_burden = _money(row.get("court_cost_burden"))
        common_expense_burden = (
            _money(row.get("ball_expense_burden"))
            + _money(row.get("other_expense_burden"))
            + _money(row.get("rain_refund_burden"))
        )
        contractor_burden = _money(row.get("contractor_cost_burden"))
        expense_total = (
            court_cost_burden + common_expense_burden + contractor_burden
        )

        profit_rows.append(
            {
                "coach": row.get("coach"),
                "coach_name": row.get("coach_name", "-"),
                "ticket_revenue": ticket_revenue,
                "cash_revenue": cash_revenue,
                "stringing_revenue": stringing_revenue,
                "revenue_total": revenue_total,
                "court_cost_burden": court_cost_burden,
                "common_expense_burden": common_expense_burden,
                "contractor_burden": contractor_burden,
                "expense_total": expense_total,
                "monthly_profit": revenue_total - expense_total,
            }
        )
    return profit_rows
