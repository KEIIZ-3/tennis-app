import json
import os
from datetime import datetime, timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .forms import CoachAvailabilityForm, LoginForm, ReservationCreateForm
from .models import CoachAvailability, LineAccountLink, Reservation
from .services.notifications import (
    build_reservation_canceled_message,
    build_reservation_created_message,
    notify_user,
    verify_line_signature,
)

User = get_user_model()


def _is_coach_or_admin(user):
    return user.is_authenticated and (user.is_superuser or getattr(user, "role", "") == "coach")


def _color_for_coach(coach_id):
    palette = [
        "#2563eb",
        "#16a34a",
        "#7c3aed",
        "#ea580c",
        "#0891b2",
        "#db2777",
        "#4f46e5",
        "#65a30d",
    ]
    return palette[coach_id % len(palette)]


def _parse_iso_datetime(value):
    if not value:
        return None

    value = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def _hour_slots(start_at, end_at):
    current = start_at
    while current < end_at:
        next_time = current + timedelta(hours=1)
        yield current, next_time
        current = next_time


def home(request):
    if not request.user.is_authenticated:
        return redirect("club:login")

    coaches = User.objects.filter(role="coach").order_by("username")
    return render(
        request,
        "home.html",
        {
            "coaches": coaches,
            "is_coach_or_admin": _is_coach_or_admin(request.user),
        },
    )


def login_view(request):
    if request.user.is_authenticated:
        return redirect("club:home")

    form = LoginForm(request.POST or None)
    error = None

    if request.method == "POST":
        if form.is_valid():
            username = form.cleaned_data["username"]
            password = form.cleaned_data["password"]
        else:
            username = request.POST.get("username", "")
            password = request.POST.get("password", "")

        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect("club:home")

        error = "ログインに失敗しました。"
        messages.error(request, error)

    return render(
        request,
        "login.html",
        {
            "form": form,
            "error": error,
        },
    )


@login_required
def logout_view(request):
    logout(request)
    return redirect("club:login")


def healthz(request):
    return HttpResponse("ok")


@login_required
@require_GET
def calendar_events(request):
    start = _parse_iso_datetime(request.GET.get("start"))
    end = _parse_iso_datetime(request.GET.get("end"))
    coach_filter = request.GET.get("coach") or request.GET.get("coach_id") or "all"

    if not start or not end:
        return JsonResponse([], safe=False)

    availabilities = CoachAvailability.objects.select_related("coach", "court").filter(
        start_at__lt=end,
        end_at__gt=start,
    )

    if coach_filter != "all":
        availabilities = availabilities.filter(coach_id=coach_filter)

    active_reservations = Reservation.objects.select_related("user", "coach", "court").filter(
        status=Reservation.STATUS_ACTIVE,
        start_at__lt=end,
        end_at__gt=start,
    )

    reservation_count_map = {}
    for reservation in active_reservations:
        key = (reservation.coach_id, reservation.court_id, reservation.start_at, reservation.end_at)
        reservation_count_map[key] = reservation_count_map.get(key, 0) + 1

    my_reservations = active_reservations.filter(user=request.user)

    events = []

    for availability in availabilities:
        base_color = _color_for_coach(availability.coach_id)

        for slot_start, slot_end in _hour_slots(availability.start_at, availability.end_at):
            if slot_end <= start or slot_start >= end:
                continue

            count = reservation_count_map.get(
                (availability.coach_id, availability.court_id, slot_start, slot_end),
                0,
            )
            status_label = "満員" if count >= availability.capacity else "空き"

            params = urlencode(
                {
                    "coach": availability.coach_id,
                    "court": availability.court_id,
                    "start_at": slot_start.isoformat(),
                    "end_at": slot_end.isoformat(),
                }
            )

            title = (
                f"{availability.coach.username}\n"
                f"{availability.court.name}\n"
                f"{status_label} {count}/{availability.capacity}"
            )

            background = "#dc2626" if status_label == "満員" else base_color

            events.append(
                {
                    "id": f"slot-{availability.id}-{slot_start.isoformat()}",
                    "title": title,
                    "start": slot_start.isoformat(),
                    "end": slot_end.isoformat(),
                    "backgroundColor": background,
                    "borderColor": background,
                    "textColor": "#ffffff",
                    "extendedProps": {
                        "eventType": "availability_slot",
                        "coachId": availability.coach_id,
                        "coachName": availability.coach.username,
                        "courtId": availability.court_id,
                        "courtName": availability.court.name,
                        "capacity": availability.capacity,
                        "reservedCount": count,
                        "statusLabel": status_label,
                        "note": availability.note,
                        "bookable": status_label == "空き",
                        "reservationUrl": f"{reverse('club:reservation_create')}?{params}",
                    },
                }
            )

    for reservation in my_reservations:
        events.append(
            {
                "id": f"my-reservation-{reservation.id}",
                "title": f"あなたの予約\n{reservation.coach.username}\n{reservation.court.name}",
                "start": reservation.start_at.isoformat(),
                "end": reservation.end_at.isoformat(),
                "backgroundColor": "#1d4ed8",
                "borderColor": "#1d4ed8",
                "textColor": "#ffffff",
                "extendedProps": {
                    "eventType": "my_reservation",
                    "coachName": reservation.coach.username,
                    "courtName": reservation.court.name,
                    "statusLabel": "予約済み",
                    "reservationId": reservation.id,
                    "cancelUrl": reverse("club:reservation_cancel", args=[reservation.id]),
                },
            }
        )

    return JsonResponse(events, safe=False)


@login_required
def reservation_create(request):
    initial = {}

    coach_id = request.GET.get("coach")
    court_id = request.GET.get("court")
    start_at = request.GET.get("start_at")
    end_at = request.GET.get("end_at")

    if coach_id:
        initial["coach"] = coach_id
    if court_id:
        initial["court"] = court_id
    if start_at:
        initial["start_at"] = _parse_iso_datetime(start_at)
    if end_at:
        initial["end_at"] = _parse_iso_datetime(end_at)

    if request.method == "POST":
        form = ReservationCreateForm(request.POST, request_user=request.user)
        if form.is_valid():
            reservation = form.save(commit=False)
            reservation.user = request.user
            reservation.save()

            subject, message_text = build_reservation_created_message(reservation)
            notify_user(request.user, subject, message_text)

            if reservation.coach != request.user:
                notify_user(
                    reservation.coach,
                    "【テニスクラブ】新しい予約が入りました",
                    message_text,
                )

            messages.success(request, "予約を作成しました。")
            return redirect("club:reservation_list")
    else:
        form = ReservationCreateForm(initial=initial, request_user=request.user)

    return render(request, "reservations/create.html", {"form": form})


@login_required
def reservation_list(request):
    reservations = Reservation.objects.select_related("coach", "court").filter(
        user=request.user
    ).order_by("-start_at")
    return render(request, "reservations/list.html", {"reservations": reservations})


@login_required
@require_POST
def reservation_cancel(request, pk):
    reservation = get_object_or_404(
        Reservation.objects.select_related("coach", "court"),
        pk=pk,
        user=request.user,
        status=Reservation.STATUS_ACTIVE,
    )

    reservation.status = Reservation.STATUS_CANCELED
    reservation.save(update_fields=["status"])

    subject, message_text = build_reservation_canceled_message(reservation)
    notify_user(request.user, subject, message_text)

    if reservation.coach != request.user:
        notify_user(
            reservation.coach,
            "【テニスクラブ】予約キャンセルがありました",
            message_text,
        )

    messages.success(request, "予約をキャンセルしました。")
    return redirect("club:reservation_list")


@login_required
def coach_availability_list(request):
    if not _is_coach_or_admin(request.user):
        messages.error(request, "権限がありません。")
        return redirect("club:home")

    availabilities = CoachAvailability.objects.select_related("coach", "court").all()

    if not request.user.is_superuser and getattr(request.user, "role", "") == "coach":
        availabilities = availabilities.filter(coach=request.user)

    availabilities = availabilities.order_by("start_at", "coach__username")
    return render(
        request,
        "coach/availability_list.html",
        {"availabilities": availabilities},
    )


@login_required
def coach_availability_create(request):
    if not _is_coach_or_admin(request.user):
        messages.error(request, "権限がありません。")
        return redirect("club:home")

    if request.method == "POST":
        form = CoachAvailabilityForm(request.POST, request_user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "コーチ空き時間を登録しました。")
            return redirect("club:coach_availability_list")
    else:
        form = CoachAvailabilityForm(request_user=request.user)

    return render(
        request,
        "coach/availability_create.html",
        {"form": form},
    )


@login_required
@require_POST
def coach_availability_delete(request, pk):
    if not _is_coach_or_admin(request.user):
        messages.error(request, "権限がありません。")
        return redirect("club:home")

    availability = get_object_or_404(CoachAvailability, pk=pk)

    if not request.user.is_superuser and getattr(request.user, "role", "") == "coach":
        if availability.coach_id != request.user.id:
            messages.error(request, "自分以外の空き時間は削除できません。")
            return redirect("club:coach_availability_list")

    availability.delete()
    messages.success(request, "コーチ空き時間を削除しました。")
    return redirect("club:coach_availability_list")


@login_required
def line_connect_view(request):
    linked = False
    try:
        linked = hasattr(request.user, "line_link")
    except Exception:
        linked = False

    return render(
        request,
        "line_connect.html",
        {
            "line_bot_basic_id": os.getenv("LINE_BOT_BASIC_ID", ""),
            "line_bot_invite_url": os.getenv("LINE_BOT_INVITE_URL", ""),
            "linked": linked,
        },
    )


@csrf_exempt
def line_webhook(request):
    if request.method != "POST":
        return HttpResponseBadRequest("POST only")

    body = request.body
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_line_signature(body, signature):
        return HttpResponseBadRequest("Invalid signature")

    payload = json.loads(body.decode("utf-8"))
    events = payload.get("events", [])

    for event in events:
        event_type = event.get("type")
        source = event.get("source", {})
        line_user_id = source.get("userId")

        if not line_user_id:
            continue

        if event_type == "message":
            text = event.get("message", {}).get("text", "").strip()

            if text.startswith("連携 "):
                username = text.replace("連携 ", "", 1).strip()
                user = User.objects.filter(username=username).first()
                if user:
                    LineAccountLink.objects.update_or_create(
                        user=user,
                        defaults={
                            "line_user_id": line_user_id,
                            "is_active": True,
                            "last_event_at": timezone.now(),
                        },
                    )
            else:
                link = LineAccountLink.objects.filter(line_user_id=line_user_id).first()
                if link:
                    link.last_event_at = timezone.now()
                    link.save(update_fields=["last_event_at"])

        elif event_type == "follow":
            link = LineAccountLink.objects.filter(line_user_id=line_user_id).first()
            if link:
                link.is_active = True
                link.last_event_at = timezone.now()
                link.save(update_fields=["is_active", "last_event_at"])

        elif event_type == "unfollow":
            link = LineAccountLink.objects.filter(line_user_id=line_user_id).first()
            if link:
                link.is_active = False
                link.last_event_at = timezone.now()
                link.save(update_fields=["is_active", "last_event_at"])

    return JsonResponse({"ok": True})
