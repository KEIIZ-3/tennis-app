from django.utils import timezone

from .settlement_models import CoachMonthlySettlement


def persist_monthly_settlement(
    *,
    settlement,
    coach_rows,
    ticket_purchase_total,
    preopen_paid_total,
    stringing_total,
    cash_in_total,
    salary_paid_total,
    reimbursement_paid_total,
    approved_common_expense_total,
    contractor_hourly_pay_total,
    cash_out_total,
    unpaid_salary_total,
    unpaid_reimbursement_total,
    preopen_unpaid_total,
    active_coach_ids,
    active_regular_coach_ids,
    common_expense_participant_count,
    per_coach_common_expense,
    common_expense_base_total,
):
    settlement.ticket_cash_in = ticket_purchase_total
    settlement.preopen_cash_in = preopen_paid_total
    settlement.stringing_cash_in = stringing_total
    settlement.cash_in_total = cash_in_total
    settlement.salary_cash_out = salary_paid_total
    settlement.reimbursement_cash_out = reimbursement_paid_total
    settlement.common_expense_cash_out = approved_common_expense_total
    settlement.contractor_cash_out = contractor_hourly_pay_total
    settlement.cash_out_total = cash_out_total
    settlement.unpaid_salary_total = unpaid_salary_total
    settlement.unpaid_reimbursement_total = unpaid_reimbursement_total
    settlement.uncollected_revenue_total = preopen_unpaid_total
    settlement.recalculate_closing_balance()
    settlement.updated_at = timezone.now()
    settlement.save()

    saved_coach_ids = []
    for row in coach_rows:
        saved, _created = CoachMonthlySettlement.objects.update_or_create(
            monthly_settlement=settlement,
            coach=row["coach"],
            defaults={
                "is_contractor_coach": row["is_contractor_coach"],
                "lesson_count": row["reservation_count"],
                "ticket_revenue": row["ticket_amount"],
                "preopen_paid_revenue": row["preopen_paid_amount"],
                "preopen_unpaid_revenue": row["preopen_unpaid_amount"],
                "stringing_revenue": row["stringing_amount"],
                "contractor_work_amount": row["contractor_hourly_pay_amount"],
                "common_expense_share": row["common_expense_share"],
                "reimbursement_carry_in": row["reimbursement_carry_in"],
                "reimbursement_current_month": row[
                    "reimbursement_current_month"
                ],
                "reimbursement_due": row["reimbursement_due"],
                "salary_due": row["salary_due"],
                "salary_paid": row["salary_paid"],
                "salary_unpaid": row["unpaid_salary"],
                "reimbursement_paid": row["reimbursement_paid"],
                "reimbursement_unpaid": row["unpaid_reimbursement"],
                "calculation_snapshot": {
                    "contractor_work_minutes": row[
                        "contractor_work_minutes"
                    ],
                    "contractor_work_hours_text": row[
                        "contractor_work_hours_text"
                    ],
                    "contractor_work_slot_count": row[
                        "contractor_work_slot_count"
                    ],
                    "lesson_compensation_amount": row[
                        "lesson_compensation_amount"
                    ],
                    "lesson_and_work_amount": row[
                        "lesson_and_work_amount"
                    ],
                },
                "updated_at": timezone.now(),
            },
        )
        saved_coach_ids.append(saved.pk)

    CoachMonthlySettlement.objects.filter(
        monthly_settlement=settlement
    ).exclude(pk__in=saved_coach_ids).delete()

    settlement.calculation_snapshot = {
        "active_coach_count": len(active_coach_ids),
        "active_regular_coach_ids": sorted(active_regular_coach_ids),
        "common_expense_participant_count": common_expense_participant_count,
        "per_coach_common_expense": per_coach_common_expense,
        "common_expense_base_total": common_expense_base_total,
        "contractor_hourly_pay_total": contractor_hourly_pay_total,
    }
    settlement.updated_at = timezone.now()
    settlement.save(update_fields=["calculation_snapshot", "updated_at"])
