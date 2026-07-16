from datetime import date

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .settlement_models import SettlementPayment
from .settlement_service import (
    allocate_reimbursement_fifo,
    calculate_monthly_settlement,
    display_name,
    get_or_create_monthly_settlement,
)


def _month_url(year, month):
    return (
        f"{reverse('club:coach_admin_settlement')}"
        f"?year={int(year)}&month={int(month)}"
    )


def _previous_month(year, month):
    month -= 1
    if month == 0:
        return year - 1, 12
    return year, month


def _next_month(year, month):
    month += 1
    if month == 13:
        return year + 1, 1
    return year, month


@login_required
@require_http_methods(["GET", "POST"])
def coach_admin_settlement(request):
    is_admin = bool(
        getattr(request.user, "is_superuser", False)
        or getattr(request.user, "is_staff", False)
    )
    if not is_admin:
        return HttpResponse("Forbidden", status=403)

    today = timezone.localdate()
    try:
        selected_year = int(
            request.GET.get("year")
            or request.POST.get("year")
            or today.year
        )
    except Exception:
        selected_year = today.year
    try:
        selected_month = int(
            request.GET.get("month")
            or request.POST.get("month")
            or today.month
        )
    except Exception:
        selected_month = today.month

    if selected_year < 2000 or selected_year > 2100:
        selected_year = today.year
    if selected_month < 1 or selected_month > 12:
        selected_month = today.month

    redirect_url = _month_url(selected_year, selected_month)
    settlement = get_or_create_monthly_settlement(
        selected_year,
        selected_month,
    )

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        if action == "create_payout":
            if settlement.is_closed:
                messages.error(
                    request,
                    "締め済みの月には支払いを追加できません。先に締め解除してください。",
                )
                return redirect(redirect_url)

            User = get_user_model()
            coach_id = (request.POST.get("coach_id") or "").strip()
            payout_type = (request.POST.get("payout_type") or "").strip()
            raw_amount = (request.POST.get("amount") or "").strip()
            raw_paid_date = (request.POST.get("paid_date") or "").strip()
            note = (request.POST.get("note") or "").strip()

            coach = User.objects.filter(
                pk=coach_id,
                role__in=("coach", "contractor_coach"),
            ).first()
            if not coach:
                messages.error(request, "支払先コーチを選択してください。")
                return redirect(redirect_url)

            type_map = {
                "salary_payout": SettlementPayment.PAYMENT_TYPE_SALARY,
                "reimbursement_payout": (
                    SettlementPayment.PAYMENT_TYPE_REIMBURSEMENT
                ),
                SettlementPayment.PAYMENT_TYPE_SALARY: (
                    SettlementPayment.PAYMENT_TYPE_SALARY
                ),
                SettlementPayment.PAYMENT_TYPE_REIMBURSEMENT: (
                    SettlementPayment.PAYMENT_TYPE_REIMBURSEMENT
                ),
            }
            payment_type = type_map.get(payout_type)
            if not payment_type:
                messages.error(request, "支払種別が不正です。")
                return redirect(redirect_url)

            try:
                amount = int(raw_amount or "0")
            except Exception:
                amount = 0
            if amount <= 0:
                messages.error(request, "金額は1円以上で入力してください。")
                return redirect(redirect_url)

            try:
                paid_date = (
                    date.fromisoformat(raw_paid_date)
                    if raw_paid_date
                    else today
                )
            except Exception:
                messages.error(request, "支払日の形式が正しくありません。")
                return redirect(redirect_url)

            with transaction.atomic():
                payment = SettlementPayment.objects.create(
                    monthly_settlement=settlement,
                    coach=coach,
                    payment_type=payment_type,
                    amount=amount,
                    paid_date=paid_date,
                    note=note,
                    created_by=request.user,
                )
                allocated = 0
                if (
                    payment_type
                    == SettlementPayment.PAYMENT_TYPE_REIMBURSEMENT
                ):
                    allocated = allocate_reimbursement_fifo(payment)

            if (
                payment_type
                == SettlementPayment.PAYMENT_TYPE_REIMBURSEMENT
            ):
                messages.success(
                    request,
                    (
                        f"{display_name(coach)}さんへの立替精算 "
                        f"{amount:,}円を記録し、古い承認済み経費へ"
                        f"{allocated:,}円を充当しました。"
                    ),
                )
            else:
                messages.success(
                    request,
                    (
                        f"{display_name(coach)}さんへの給与 "
                        f"{amount:,}円を記録しました。"
                    ),
                )
            calculate_monthly_settlement(
                selected_year,
                selected_month,
                force=True,
            )
            return redirect(redirect_url)

        if action == "close_month":
            result = calculate_monthly_settlement(
                selected_year,
                selected_month,
                force=True,
            )
            settlement = result["settlement"]
            settlement.close(
                user=request.user,
                snapshot={
                    "coach_rows": [
                        {
                            "coach_id": row["coach"].pk,
                            "coach_name": row["coach_name"],
                            "salary_due": row["salary_due"],
                            "salary_paid": row["salary_paid"],
                            "unpaid_salary": row["unpaid_salary"],
                            "reimbursement_due": row[
                                "reimbursement_due"
                            ],
                            "reimbursement_paid": row[
                                "reimbursement_paid"
                            ],
                            "unpaid_reimbursement": row[
                                "unpaid_reimbursement"
                            ],
                        }
                        for row in result["coach_rows"]
                    ],
                    "cash_in_total": result["cash_in_total"],
                    "cash_out_total": result["cash_out_total"],
                    "closing_balance": result["company_balance"],
                },
            )
            messages.success(
                request,
                f"{selected_year}年{selected_month}月を締めました。",
            )
            return redirect(redirect_url)

        if action == "reopen_month":
            settlement.reopen(user=request.user)
            messages.success(
                request,
                f"{selected_year}年{selected_month}月の締めを解除しました。",
            )
            return redirect(redirect_url)

        if action == "reverse_payment":
            payment_id = (request.POST.get("payment_id") or "").strip()
            payment = SettlementPayment.objects.filter(
                pk=payment_id,
                monthly_settlement=settlement,
            ).first()
            if not payment:
                messages.error(request, "対象の支払いが見つかりません。")
                return redirect(redirect_url)
            if settlement.is_closed:
                messages.error(
                    request,
                    "締め済みの月では支払いを取り消せません。",
                )
                return redirect(redirect_url)
            payment.reverse(
                user=request.user,
                note=(request.POST.get("reversal_note") or "").strip(),
            )
            calculate_monthly_settlement(
                selected_year,
                selected_month,
                force=True,
            )
            messages.success(request, "支払いを取り消しました。")
            return redirect(redirect_url)

    result = calculate_monthly_settlement(
        selected_year,
        selected_month,
    )
    settlement = result["settlement"]

    previous_year, previous_month = _previous_month(
        selected_year,
        selected_month,
    )
    next_year, next_month = _next_month(
        selected_year,
        selected_month,
    )

    User = get_user_model()
    coach_queryset = User.objects.filter(
        role__in=("coach", "contractor_coach")
    ).order_by("full_name", "username", "id")

    context = {
        **result,
        "selected_year": selected_year,
        "selected_month": selected_month,
        "month_label": f"{selected_year}年{selected_month}月",
        "prev_url": _month_url(previous_year, previous_month),
        "next_url": _month_url(next_year, next_month),
        "coach_options": coach_queryset,
        "today_value": today.isoformat(),
        "payout_type_choices": [
            ("salary_payout", "給与支払い"),
            ("reimbursement_payout", "本人立替精算支払い"),
        ],
        "settlement_status": settlement.status,
        "settlement_status_label": settlement.get_status_display(),
        "is_month_closed": settlement.is_closed,
        "opening_balance": settlement.opening_balance,
        "closing_balance": settlement.closing_balance,
    }

    if "payout_history_rows" not in context:
        from .settlement_service import payment_history_rows

        context["payout_history_rows"] = payment_history_rows(settlement)

    for key in (
        "approved_common_expense_rows",
        "approved_personal_expense_rows",
        "submitted_personal_expense_rows",
    ):
        context.setdefault(key, [])

    snapshot = settlement.calculation_snapshot or {}
    context.setdefault(
        "common_expense_participant_count",
        snapshot.get("common_expense_participant_count", 0),
    )
    context.setdefault(
        "per_coach_common_expense",
        snapshot.get("per_coach_common_expense", 0),
    )
    context.setdefault(
        "common_expense_base_total",
        snapshot.get("common_expense_base_total", 0),
    )
    context.setdefault(
        "contractor_hourly_pay_total",
        snapshot.get("contractor_hourly_pay_total", 0),
    )
    context.setdefault("active_coach_count", snapshot.get("active_coach_count", 0))

    for key in (
        "preopen_paid_total",
        "preopen_unpaid_total",
        "ticket_amount_total",
        "ticket_purchase_total",
        "stringing_total",
        "cash_in_total",
        "approved_common_expense_total",
        "salary_due_total",
        "reimbursement_due_total",
        "salary_paid_total",
        "reimbursement_paid_total",
        "unpaid_salary_total",
        "unpaid_reimbursement_total",
        "pending_personal_reimbursement_total",
        "cash_out_total",
        "company_balance",
    ):
        context.setdefault(key, 0)

    return render(
        request,
        "coach/admin_settlement.html",
        context,
    )
