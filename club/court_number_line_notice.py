from collections import OrderedDict

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .models import Reservation
from .notifications import notify_user_line_only


def _is_coach_like(user):
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and (
            getattr(user, "is_staff", False)
            or getattr(user, "is_superuser", False)
            or getattr(user, "role", "") in ("coach", "contractor_coach")
        )
    )


def _display_name(user):
    try:
        return user.display_name()
    except Exception:
        return getattr(user, "username", "-") or "-"


def _local(value):
    try:
        return timezone.localtime(value) if timezone.is_aware(value) else value
    except Exception:
        return value


def _slot_key(reservation):
    return (
        reservation.lesson_type,
        reservation.coach_id,
        reservation.court_id,
        reservation.start_at,
        reservation.end_at,
    )


def _slots_for_user(user):
    qs = (
        Reservation.objects.filter(
            status=Reservation.STATUS_ACTIVE,
            end_at__gte=timezone.now(),
        )
        .select_related("coach", "substitute_coach", "court")
        .order_by("start_at", "coach_id", "court_id", "id")
    )
    if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
        qs = qs.filter(Q(coach=user) | Q(substitute_coach=user))

    slots = OrderedDict()
    for reservation in qs:
        slots.setdefault(_slot_key(reservation), reservation)
    return list(slots.values())


def _slot_participants(slot):
    return (
        Reservation.objects.filter(
            status=Reservation.STATUS_ACTIVE,
            lesson_type=slot.lesson_type,
            coach=slot.coach,
            court=slot.court,
            start_at=slot.start_at,
            end_at=slot.end_at,
        )
        .select_related("user")
        .order_by("user__full_name", "user__username", "id")
    )


def _line_ready(user):
    link = getattr(user, "line_link", None)
    return bool(
        link
        and getattr(link, "is_active", False)
        and (getattr(link, "line_user_id", "") or "").strip()
    )


def _message_text(slot, court_number, note):
    start_at = _local(slot.start_at)
    end_at = _local(slot.end_at)
    coach = getattr(slot, "substitute_coach", None) or slot.coach
    lines = [
        "【Play Design Tennis】本日のコート番号のお知らせ",
        "",
        f"日時：{start_at:%Y/%m/%d（%a） %H:%M}〜{end_at:%H:%M}",
        f"レッスン：{slot.get_lesson_type_display()}",
        f"担当コーチ：{_display_name(coach)}",
        f"コート番号：{court_number}",
    ]
    if note:
        lines += ["", f"連絡事項：{note}"]
    lines += ["", "お気をつけてお越しください。"]
    return "\n".join(lines)


@login_required
@require_http_methods(["GET", "POST"])
def court_number_line_notice(request):
    if not _is_coach_like(request.user):
        return HttpResponse("Forbidden", status=403)

    slots = _slots_for_user(request.user)
    slot_map = {str(slot.pk): slot for slot in slots}

    selected_slot_id = (request.POST.get("slot_id") or request.GET.get("slot_id") or "").strip()
    selected_slot = slot_map.get(selected_slot_id)
    court_number = (request.POST.get("court_number") or "").strip()
    note = (request.POST.get("note") or "").strip()

    rows = []
    preview = ""
    line_ready_count = 0

    if selected_slot:
        for reservation in _slot_participants(selected_slot):
            ready = _line_ready(reservation.user)
            if ready:
                line_ready_count += 1
            rows.append({"name": _display_name(reservation.user), "line_ready": ready})

        if court_number:
            preview = _message_text(selected_slot, court_number, note)

    if request.method == "POST" and request.POST.get("action") == "send":
        if not selected_slot:
            messages.error(request, "対象レッスンを選択してください。")
        elif not court_number:
            messages.error(request, "コート番号を入力してください。")
        elif request.POST.get("confirm_send") != "yes":
            messages.error(request, "送信前確認にチェックしてください。")
        else:
            sent = 0
            failed = 0
            message_text = _message_text(selected_slot, court_number, note)
            for reservation in _slot_participants(selected_slot):
                result = notify_user_line_only(
                    reservation.user,
                    message_text,
                    subject="Play Design Tennis コート番号のお知らせ",
                )
                if result.get("line"):
                    sent += 1
                else:
                    failed += 1

            if sent:
                messages.success(request, f"コート番号を {sent} 名へLINE送信しました。")
            if failed:
                messages.warning(request, f"{failed} 名にはLINE送信できませんでした。LINE連携状況をご確認ください。")
            return redirect(f"{reverse('club:court_number_line_notice')}?slot_id={selected_slot.pk}")

    return render(
        request,
        "coach/court_number_line_notice.html",
        {
            "slots": slots,
            "selected_slot": selected_slot,
            "selected_slot_id": selected_slot_id,
            "court_number": court_number,
            "note": note,
            "participant_rows": rows,
            "line_ready_count": line_ready_count,
            "message_preview": preview,
        },
    )
