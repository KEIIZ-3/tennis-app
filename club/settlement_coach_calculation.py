from django.utils import timezone

from .settlement_models import CoachMonthlySettlement


def calculate_coach_wallets(
    *,
    coach_rows,
    settlement,
    main_coach_ids,
    court_policy,
    other_expense_policy,
    rain_refund_policy,
    contractor_share_by_main,
    negative_carry_in_by_coach,
    unpaid_salary_carry_in_by_coach,
    total_company_revenue,
    money,
    active_salary_payment_total,
    active_reimbursement_payment_total,
):
    main_coach_id_set = set(main_coach_ids)
    final_total_before_adjustment = 0

    for row in coach_rows:
        coach = row.get("coach")
        coach_id = getattr(coach, "pk", None)
        is_contractor = bool(row.get("is_contractor_coach"))
        lesson_revenue = (
            money(row.get("ticket_amount"))
            + money(row.get("preopen_paid_amount"))
        )
        stringing_revenue = money(row.get("stringing_amount"))
        court_burden = money(court_policy["burden_by_coach"].get(coach_id))
        ball_expense_burden = money(
            other_expense_policy.get("ball_burden_by_coach", {}).get(coach_id)
        )
        other_expense_burden = money(
            other_expense_policy.get(
                "other_burden_by_coach",
                other_expense_policy["burden_by_coach"],
            ).get(coach_id)
        )
        contractor_burden = money(contractor_share_by_main.get(coach_id))
        rain_refund_burden = money(
            rain_refund_policy["burden_by_coach"].get(coach_id)
        )
        court_reimbursement = money(
            court_policy["reimbursement_by_coach"].get(coach_id)
        )
        ball_expense_reimbursement = money(
            other_expense_policy.get(
                "ball_reimbursement_by_coach", {}
            ).get(coach_id)
        )
        other_expense_reimbursement = money(
            other_expense_policy.get(
                "other_reimbursement_by_coach",
                other_expense_policy["reimbursement_by_coach"],
            ).get(coach_id)
        )
        rain_refund_reimbursement = money(
            rain_refund_policy["reimbursement_by_coach"].get(coach_id)
        )
        reimbursement_total = (
            court_reimbursement
            + ball_expense_reimbursement
            + other_expense_reimbursement
            + rain_refund_reimbursement
        )
        negative_carry_in = money(negative_carry_in_by_coach.get(coach_id))
        unpaid_salary_carry_in = money(
            unpaid_salary_carry_in_by_coach.get(coach_id)
        )

        if is_contractor:
            earned_amount = (
                money(row.get("contractor_hourly_pay_amount"))
                + stringing_revenue
            )
            burden_total = 0
        else:
            earned_amount = lesson_revenue + stringing_revenue
            burden_total = (
                court_burden
                + ball_expense_burden
                + other_expense_burden
                + contractor_burden
                + rain_refund_burden
            )

        final_entitlement = (
            earned_amount
            + reimbursement_total
            + unpaid_salary_carry_in
            - burden_total
            - negative_carry_in
        )
        row.update(
            {
                "is_main_coach": coach_id in main_coach_id_set,
                "company_revenue_contribution": lesson_revenue + stringing_revenue,
                "court_cost_burden": court_burden,
                "ball_expense_burden": ball_expense_burden,
                "other_expense_burden": other_expense_burden,
                "contractor_cost_burden": contractor_burden,
                "rain_refund_burden": rain_refund_burden,
                "total_cost_burden": burden_total,
                "court_reimbursement": court_reimbursement,
                "ball_expense_reimbursement": ball_expense_reimbursement,
                "other_expense_reimbursement": other_expense_reimbursement,
                "rain_refund_reimbursement": rain_refund_reimbursement,
                "wallet_reimbursement": reimbursement_total,
                "wallet_earned_amount": earned_amount,
                "negative_carry_in": negative_carry_in,
                "unpaid_salary_carry_in": unpaid_salary_carry_in,
                "wallet_final_entitlement": final_entitlement,
                "wallet_balance_adjustment": 0,
            }
        )
        final_total_before_adjustment += final_entitlement

    wallet_difference = total_company_revenue - final_total_before_adjustment
    salary_due_total = 0
    salary_paid_total = 0
    reimbursement_paid_total = 0
    unpaid_salary_total = 0
    negative_carry_total = 0

    for row in coach_rows:
        coach = row.get("coach")
        final_entitlement = money(row.get("wallet_final_entitlement"))
        salary_paid = active_salary_payment_total(settlement, coach)
        reimbursement_paid = active_reimbursement_payment_total(
            settlement, coach
        )
        total_paid = salary_paid + reimbursement_paid
        salary_due = max(final_entitlement, 0)
        closing_balance = final_entitlement - total_paid
        unpaid_salary = max(closing_balance, 0)
        negative_carry = max(-closing_balance, 0)
        row.update(
            {
                "salary_due": salary_due,
                "salary_paid": salary_paid,
                "unpaid_salary": unpaid_salary,
                "negative_carry": negative_carry,
                "closing_compensation_balance": closing_balance,
                "personal_reimbursement_due": money(
                    row.get("wallet_reimbursement")
                ),
                "reimbursement_due": money(row.get("wallet_reimbursement")),
                "reimbursement_paid": reimbursement_paid,
                "unpaid_reimbursement": 0,
                "total_unpaid": unpaid_salary,
                "total_paid": total_paid,
                "common_expense_share": money(row.get("total_cost_burden")),
            }
        )
        saved_row = CoachMonthlySettlement.objects.filter(
            monthly_settlement=settlement, coach=coach
        ).first()
        if saved_row is not None:
            snapshot = dict(saved_row.calculation_snapshot or {})
            snapshot.update(
                {
                    "wallet_policy": True,
                    "is_main_coach": bool(row.get("is_main_coach")),
                    "company_revenue_contribution": money(
                        row.get("company_revenue_contribution")
                    ),
                    "court_cost_burden": money(row.get("court_cost_burden")),
                    "ball_expense_burden": money(
                        row.get("ball_expense_burden")
                    ),
                    "other_expense_burden": money(
                        row.get("other_expense_burden")
                    ),
                    "contractor_cost_burden": money(
                        row.get("contractor_cost_burden")
                    ),
                    "rain_refund_burden": money(
                        row.get("rain_refund_burden")
                    ),
                    "total_cost_burden": money(row.get("total_cost_burden")),
                    "court_reimbursement": money(
                        row.get("court_reimbursement")
                    ),
                    "ball_expense_reimbursement": money(
                        row.get("ball_expense_reimbursement")
                    ),
                    "other_expense_reimbursement": money(
                        row.get("other_expense_reimbursement")
                    ),
                    "rain_refund_reimbursement": money(
                        row.get("rain_refund_reimbursement")
                    ),
                    "wallet_reimbursement": money(
                        row.get("wallet_reimbursement")
                    ),
                    "wallet_earned_amount": money(
                        row.get("wallet_earned_amount")
                    ),
                    "negative_carry_in": money(row.get("negative_carry_in")),
                    "unpaid_salary_carry_in": money(
                        row.get("unpaid_salary_carry_in")
                    ),
                    "wallet_balance_adjustment": money(
                        row.get("wallet_balance_adjustment")
                    ),
                    "wallet_final_entitlement": final_entitlement,
                    "closing_compensation_balance": closing_balance,
                    "negative_carry": negative_carry,
                }
            )
            saved_row.common_expense_share = money(
                row.get("total_cost_burden")
            )
            saved_row.reimbursement_due = money(
                row.get("wallet_reimbursement")
            )
            saved_row.reimbursement_current_month = money(
                row.get("wallet_reimbursement")
            )
            saved_row.reimbursement_carry_in = 0
            saved_row.salary_due = salary_due
            saved_row.salary_paid = salary_paid
            saved_row.salary_unpaid = unpaid_salary
            saved_row.reimbursement_paid = reimbursement_paid
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
        reimbursement_paid_total += reimbursement_paid
        unpaid_salary_total += unpaid_salary
        negative_carry_total += negative_carry

    return {
        "coach_rows": coach_rows,
        "final_total_before_adjustment": final_total_before_adjustment,
        "wallet_difference": wallet_difference,
        "salary_due_total": salary_due_total,
        "salary_paid_total": salary_paid_total,
        "reimbursement_paid_total": reimbursement_paid_total,
        "unpaid_salary_total": unpaid_salary_total,
        "negative_carry_total": negative_carry_total,
    }
