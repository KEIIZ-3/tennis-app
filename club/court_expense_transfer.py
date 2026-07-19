import json
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
    using_coach_ids = {coach.pk for coach in using_coaches}
    is_full_admin = bool(request.user.is_staff or request.user.is_superuser)
    if not is_full_admin and request.user.pk not in using_coach_ids:
        return HttpResponse("Forbidden", status=403)

    User = get_user_model()
    payer_options = User.objects.filter(
        pk__in=using_coach_ids,
        role__in=User.COACH_ROLE_VALUES,
        is_active=True,
    ).order_by(
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
