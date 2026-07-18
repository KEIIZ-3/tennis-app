from collections import OrderedDict
from hashlib import sha256

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .models import Reservation
from .notifications import notify_user_email_only, notify_user_line_only


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
    if reservation.fixed_lesson_id:
        return (
            "fixed_lesson",
            reservation.fixed_lesson_id,
            reservation.lesson_type,
            reservation.start_at,
            reservation.end_at,
        )
    if reservation.availability_id:
        return (
            "availability",
            reservation.availability_id,
            reservation.lesson_type,
            reservation.start_at,
            reservation.end_at,
        )
    return (
        "slot",
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
    qs = Reservation.objects.filter(
        status=Reservation.STATUS_ACTIVE,
        lesson_type=slot.lesson_type,
        start_at=slot.start_at,
        end_at=slot.end_at,
    )
    if slot.fixed_lesson_id:
        qs = qs.filter(fixed_lesson_id=slot.fixed_lesson_id)
    elif slot.availability_id:
        qs = qs.filter(availability_id=slot.availability_id)
    else:
        qs = qs.filter(coach=slot.coach, court=slot.court)
    return (
        qs
        .select_related("user")
        .order_by("user__full_name", "user__username", "id")
    )


def _selected_slot_for_user(user, slot_id):
    if not slot_id:
        return None

    qs = Reservation.objects.filter(
        pk=slot_id,
        status=Reservation.STATUS_ACTIVE,
        end_at__gte=timezone.now(),
    ).select_related(
        "coach",
        "substitute_coach",
        "court",
        "availability",
        "fixed_lesson",
    )
    if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
        qs = qs.filter(Q(coach=user) | Q(substitute_coach=user))
    return qs.first()


def _line_ready(user):
    link = getattr(user, "line_link", None)
    return bool(
        link
        and getattr(link, "is_active", False)
        and (getattr(link, "line_user_id", "") or "").strip()
    )


def _email_ready(user):
    return bool((getattr(user, "email", "") or "").strip())


def _court_place_name(court):
    if not court:
        return "現地"

    court_type = str(getattr(court, "court_type", "") or "").strip()
    court_type_labels = {
        "sono": "西猪名公園テニスコート",
        "amagasaki": "尼崎記念公園テニスコート",
        "other": "その他テニスコート",
    }
    place_name = court_type_labels.get(court_type, "").strip()
    if place_name:
        return place_name

    court_name = str(court or "").strip()
    if court_name:
        return court_name

    return "現地"


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
        f"テニスコート：{_court_place_name(slot.court)}",
        f"コート番号：{court_number}",
    ]
    if note:
        lines += ["", f"連絡事項：{note}"]
    lines += ["", "お気をつけてお越しください。"]
    return "\n".join(lines)


def _delivery_cache_key(slot, message_text):
    slot_identity = (
        f"{slot.fixed_lesson_id or 0}:"
        f"{slot.availability_id or 0}:"
        f"{slot.start_at.isoformat()}:"
        f"{slot.end_at.isoformat()}"
    )
    digest = sha256(f"{slot_identity}\n{message_text}".encode("utf-8")).hexdigest()
    return f"court-number-line-delivery:{digest}"


def _acquire_delivery_lock(cache_key):
    try:
        return cache.add(cache_key, "sending", timeout=120)
    except Exception:
        # LINE連絡自体は止めず、キャッシュ障害時だけ従来動作へ戻します。
        return True


def _finish_delivery_lock(cache_key, sent):
    try:
        if sent:
            cache.set(cache_key, "sent", timeout=120)
        else:
            cache.delete(cache_key)
    except Exception:
        pass


@login_required
@require_http_methods(["GET", "POST"])
def court_number_line_notice(request):
    if not _is_coach_like(request.user):
        return HttpResponse("Forbidden", status=403)

    slots = _slots_for_user(request.user)
    slot_map = {str(slot.pk): slot for slot in slots}

    selected_slot_id = (request.POST.get("slot_id") or request.GET.get("slot_id") or "").strip()
    selected_slot = slot_map.get(selected_slot_id) or _selected_slot_for_user(
        request.user,
        selected_slot_id,
    )
    if selected_slot:
        selected_slot_id = str(selected_slot.pk)
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
            rows.append(
                {
                    "name": _display_name(reservation.user),
                    "line_ready": ready,
                    "email_ready": _email_ready(reservation.user),
                }
            )

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
            line_sent = 0
            email_sent = 0
            failed = 0
            message_text = _message_text(selected_slot, court_number, note)
            delivery_cache_key = _delivery_cache_key(selected_slot, message_text)
            if not _acquire_delivery_lock(delivery_cache_key):
                messages.warning(
                    request,
                    "同じ内容はすでに送信処理済みです。連続送信を防止しました。",
                )
                return redirect(
                    f"{reverse('club:court_number_line_notice')}?slot_id={selected_slot.pk}"
                )

            for reservation in _slot_participants(selected_slot):
                result = notify_user_line_only(
                    reservation.user,
                    message_text,
                    subject="Play Design Tennis コート番号のお知らせ",
                )
                if result.get("line"):
                    line_sent += 1
                else:
                    email_result = notify_user_email_only(
                        reservation.user,
                        message_text,
                        subject="Play Design Tennis コート番号のお知らせ",
                    )
                    if email_result.get("email"):
                        email_sent += 1
                    else:
                        failed += 1

            delivered = line_sent + email_sent
            _finish_delivery_lock(delivery_cache_key, delivered)

            if line_sent:
                messages.success(
                    request,
                    f"コート番号を {line_sent} 名へLINE送信しました。",
                )
            if email_sent:
                messages.success(
                    request,
                    f"LINE送信できなかった {email_sent} 名へメールで補完連絡しました。",
                )
            if failed:
                messages.warning(
                    request,
                    f"{failed} 名にはLINE・メールとも送信できませんでした。連絡先をご確認ください。",
                )
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
