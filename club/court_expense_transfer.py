import json
from datetime import date

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .models import CoachAvailability, CoachExpense, Reservation

EXPENSE_NOTE_META_PREFIX = "__EXPENSE_META__"
RECORD_KIND = "court_transfer"
APPROVAL_APPROVED = "approved"
APPROVAL_REFUND_PENDING = "refund_pending"
APPROVAL_REFUNDED = "refunded"


def _display_name(user):
    if not user:
        return "-"
    try:
        return user.display_name()
    except Exception:
        return str(user)


def _is_allowed(user):
    return bool(
        getattr(user, "is_staff", False)
        or getattr(user, "is_superuser", False)
        or str(getattr(user, "role", "") or "") in ("coach", "contractor_coach")
    )


def _local(value):
    if value and timezone.is_aware(value):
        return timezone.localtime(value)
    return value


def _parse_note(note):
    text = str(note or "")
    if not text.startswith(EXPENSE_NOTE_META_PREFIX):
        return {}
    first_line = text.split("\n", 1)[0]
    raw = first_line[len(EXPENSE_NOTE_META_PREFIX):].strip()
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}


def _build_note(meta, plain_note=""):
    return (
        f"{EXPENSE_NOTE_META_PREFIX}"
        f"{json.dumps(meta, ensure_ascii=False)}\n"
        f"{str(plain_note or '').strip()}"
    )


def _facility_key(court):
    if not court:
        return "facility:unknown"
    court_type = str(getattr(court, "court_type", "") or "").strip()
    if court_type:
        return f"facility:{court_type}"
    return f"facility_name:{str(court)}"


def _facility_label(court):
    if not court:
        return "現地"
    label_map = {
        "sono": "西猪名公園テニスコート",
        "amagasaki": "尼崎記念公園テニスコート",
        "other": "その他テニスコート",
    }
    court_type = str(getattr(court, "court_type", "") or "").strip()
    return label_map.get(court_type, str(court))


def _slot_key(availability):
    start = _local(availability.start_at)
    end = _local(availability.end_at)
    return (
        f"{start.date().isoformat()}|{_facility_key(availability.court)}|"
        f"{start:%H:%M}|{end:%H:%M}"
    )


def _lesson_label(availability):
    start = _local(availability.start_at)
    end = _local(availability.end_at)
    return (
        f"{start:%Y/%m/%d} {start:%H:%M}〜{end:%H:%M} / "
        f"{_facility_label(availability.court)} / "
        f"{availability.get_lesson_type_display()}"
    )


def _using_coaches(availability):
    coaches = []
    seen = set()

    reservations = (
        Reservation.objects.filter(
            availability=availability,
            start_at=availability.start_at,
            end_at=availability.end_at,
        )
        .select_related(
            "coach",
            "substitute_coach",
            "fixed_lesson",
            "fixed_lesson__coach",
            "fixed_lesson__coach_2",
            "fixed_lesson__coach_3",
        )
        .order_by("id")
    )

    for reservation in reservations:
        if reservation.status not in (
            Reservation.STATUS_ACTIVE,
            Reservation.STATUS_PENDING,
        ):
            continue
        substitute = getattr(reservation, "substitute_coach", None)
        if substitute:
            candidates = [substitute]
        else:
            fixed_lesson = getattr(reservation, "fixed_lesson", None)
            if fixed_lesson:
                try:
                    candidates = list(fixed_lesson.all_coaches())
                except Exception:
                    candidates = [reservation.coach]
            else:
                candidates = [reservation.coach]

        for coach in candidates:
            if not coach or coach.pk in seen:
                continue
            if getattr(coach, "role", "") not in ("coach", "contractor_coach"):
                continue
            seen.add(coach.pk)
            coaches.append(coach)

    if not coaches:
        coach = availability.substitute_coach or availability.coach
        if coach:
            coaches.append(coach)

    return coaches


def _month_range(year, month):
    start = date(int(year), int(month), 1)
    if int(month) == 12:
        end = date(int(year) + 1, 1, 1)
    else:
        end = date(int(year), int(month) + 1, 1)
    return start, end


def _transfer_expenses(year, month):
    start, end = _month_range(year, month)
    rows = []
    for expense in CoachExpense.objects.filter(
        expense_date__gte=start,
        expense_date__lt=end,
        category=CoachExpense.CATEGORY_COURT,
    ).select_related("created_by"):
        meta = _parse_note(expense.note)
        if meta.get("record_kind") != RECORD_KIND:
            continue
        if meta.get("approval_status") in (
            APPROVAL_REFUND_PENDING,
            APPROVAL_REFUNDED,
        ):
            continue
        rows.append((expense, meta))
    return rows


def _apply_transfer_adjustments(result, year, month):
    if not isinstance(result, dict):
        return result

    coach_rows = result.get("coach_rows") or []
    row_map = {
        getattr(row.get("coach"), "pk", None): row
        for row in coach_rows
        if row.get("coach") is not None
    }

    for row in coach_rows:
        row["court_use_deduction"] = 0
        row["court_advance_credit"] = 0
        row["court_transfer_net"] = 0

    for expense, meta in _transfer_expenses(year, month):
        amount = max(int(expense.amount or 0), 0)
        payer_id = meta.get("payer_coach_id")
        using_ids = []
        for value in meta.get("using_coach_ids") or []:
            try:
                coach_id = int(value)
            except Exception:
                continue
            if coach_id in row_map and coach_id not in using_ids:
                using_ids.append(coach_id)

        if not using_ids or amount <= 0:
            continue

        base = amount // len(using_ids)
        remainder = amount % len(using_ids)
        for index, coach_id in enumerate(using_ids):
            deduction = base + (1 if index < remainder else 0)
            row_map[coach_id]["court_use_deduction"] += deduction

        try:
            payer_id = int(payer_id)
        except Exception:
            payer_id = None
        if payer_id in row_map:
            row_map[payer_id]["court_advance_credit"] += amount

    salary_due_total = 0
    unpaid_salary_total = 0
    for row in coach_rows:
        deduction = int(row.get("court_use_deduction") or 0)
        credit = int(row.get("court_advance_credit") or 0)
        row["court_transfer_net"] = credit - deduction

        base_salary_due = int(row.get("salary_due") or 0)
        adjusted_salary_due = max(base_salary_due - deduction + credit, 0)
        salary_paid = int(row.get("salary_paid") or 0)
        unpaid_salary = max(adjusted_salary_due - salary_paid, 0)

        row["salary_due"] = adjusted_salary_due
        row["unpaid_salary"] = unpaid_salary
        row["total_unpaid"] = unpaid_salary + int(row.get("unpaid_reimbursement") or 0)

        # 既存テンプレートの表示項目にも連動させます。
        row["court_cost_burden"] = int(row.get("court_cost_burden") or 0) + deduction
        row["wallet_reimbursement"] = int(row.get("wallet_reimbursement") or 0) + credit
        if "wallet_final_entitlement" in row:
            row["wallet_final_entitlement"] = max(
                int(row.get("wallet_final_entitlement") or 0) - deduction + credit,
                0,
            )

        salary_due_total += adjusted_salary_due
        unpaid_salary_total += unpaid_salary

    result["salary_due_total"] = salary_due_total
    result["unpaid_salary_total"] = unpaid_salary_total
    result["court_transfer_total"] = sum(
        int(row.get("court_advance_credit") or 0) for row in coach_rows
    )

    # 月次保存値にも反映し、締め後も同じ金額を表示します。
    try:
        from .settlement_models import CoachMonthlySettlement

        for row in coach_rows:
            saved = CoachMonthlySettlement.objects.filter(
                monthly_settlement=result.get("settlement"),
                coach=row.get("coach"),
            ).first()
            if not saved:
                continue
            snapshot = dict(saved.calculation_snapshot or {})
            snapshot.update(
                {
                    "court_use_deduction": row["court_use_deduction"],
                    "court_advance_credit": row["court_advance_credit"],
                    "court_transfer_net": row["court_transfer_net"],
                }
            )
            saved.salary_due = row["salary_due"]
            saved.salary_unpaid = row["unpaid_salary"]
            saved.calculation_snapshot = snapshot
            saved.updated_at = timezone.now()
            saved.save(
                update_fields=[
                    "salary_due",
                    "salary_unpaid",
                    "calculation_snapshot",
                    "updated_at",
                ]
            )
    except Exception:
        pass

    return result


def install_settlement_patch():
    from . import settlement_service

    current = settlement_service.calculate_monthly_settlement
    if getattr(current, "_court_transfer_patch", False):
        return

    def wrapped(year, month, *, force=False):
        result = current(year, month, force=force)
        return _apply_transfer_adjustments(result, year, month)

    wrapped._court_transfer_patch = True
    wrapped._original = current
    settlement_service.calculate_monthly_settlement = wrapped

    # from import 済みの参照も差し替えます。
    try:
        from . import lesson_execution
        lesson_execution.calculate_monthly_settlement = wrapped
    except Exception:
        pass
    try:
        from . import settlement_views
        settlement_views.calculate_monthly_settlement = wrapped
    except Exception:
        pass


install_settlement_patch()


@login_required
@require_http_methods(["GET", "POST"])
def coach_expense_manage(request):
    if not _is_allowed(request.user):
        return HttpResponse("Forbidden", status=403)

    availability_id = (
        request.GET.get("availability_id")
        or request.POST.get("availability_id")
        or ""
    ).strip()
    action = (request.POST.get("action") or "").strip()

    # 通常の経費管理は既存画面へ委譲します。
    if not availability_id and action != "create_court_transfer":
        from . import views
        return views.coach_expense_manage(request)

    availability = get_object_or_404(
        CoachAvailability.objects.select_related("coach", "substitute_coach", "court"),
        pk=availability_id,
    )
    using_coaches = _using_coaches(availability)
    User = get_user_model()
    payer_options = User.objects.filter(role="coach", is_active=True).order_by(
        "full_name", "username", "id"
    )

    if request.method == "POST":
        payer_id = (request.POST.get("payer_coach_id") or "").strip()
        raw_amount = (request.POST.get("amount") or "").strip()
        plain_note = (request.POST.get("note") or "").strip()
        payer = payer_options.filter(pk=payer_id).first()

        try:
            amount = int(raw_amount or "0")
        except Exception:
            amount = 0

        if not payer:
            messages.error(request, "コート代を支払ったメインコーチを選択してください。")
        elif amount <= 0:
            messages.error(request, "コート代は1円以上で入力してください。")
        elif not using_coaches:
            messages.error(request, "このレッスンの利用コーチを特定できませんでした。")
        else:
            start = _local(availability.start_at)
            meta = {
                "expense_type": "court_transfer",
                "receipt_status": "none",
                "receipt_check_status": "checked",
                "approval_status": APPROVAL_APPROVED,
                "record_kind": RECORD_KIND,
                "availability_id": availability.pk,
                "court_refund_slot_key": _slot_key(availability),
                "court_refund_lesson_label": _lesson_label(availability),
                "court_refund_facility_label": _facility_label(availability.court),
                "payer_coach_id": payer.pk,
                "payer_coach_name": _display_name(payer),
                "using_coach_ids": [coach.pk for coach in using_coaches],
                "using_coach_names": [_display_name(coach) for coach in using_coaches],
                "recorded_by_id": request.user.pk,
                "recorded_by_name": _display_name(request.user),
            }
            with transaction.atomic():
                CoachExpense.objects.create(
                    expense_date=start.date(),
                    category=CoachExpense.CATEGORY_COURT,
                    amount=amount,
                    note=_build_note(meta, plain_note),
                    created_by=payer,
                )
                from .settlement_service import calculate_monthly_settlement
                calculate_monthly_settlement(start.year, start.month, force=True)

            messages.success(
                request,
                f"コート代{amount:,}円を登録しました。利用コーチから控除し、{_display_name(payer)}コーチへ加算します。",
            )
            return redirect("club:coach_admin_settlement")

    return render(
        request,
        "coach/court_expense_transfer.html",
        {
            "availability": availability,
            "lesson_label": _lesson_label(availability),
            "using_coaches": using_coaches,
            "payer_options": payer_options,
            "expense_date": _local(availability.start_at).date().isoformat(),
        },
    )
