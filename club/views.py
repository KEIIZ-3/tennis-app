from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .models import Reservation, CoachAvailability
from .forms import ReservationForm, CoachAvailabilityForm


User = get_user_model()


# =========================
# 基本
# =========================

def login_view(request):
    if request.user.is_authenticated:
        return redirect("club:home")

    form = AuthenticationForm(request, data=request.POST or None)

    if request.method == "POST" and form.is_valid():
        login(request, form.get_user())
        return redirect("club:home")

    return render(request, "registration/login.html", {"form": form})


def logout_view(request):
    logout(request)
    return redirect("club:login")


@login_required
def home(request):
    coaches = User.objects.filter(role="coach") if hasattr(User, "role") else User.objects.all()

    return render(request, "home.html", {
        "coaches": coaches
    })


def healthz(request):
    return HttpResponse("ok")


# =========================
# カレンダー
# =========================

@login_required
def calendar_events(request):
    events = []

    availabilities = CoachAvailability.objects.all()
    reservations = Reservation.objects.all()

    for a in availabilities:
        start = getattr(a, "start_at", None) or getattr(a, "start", None)
        end = getattr(a, "end_at", None) or getattr(a, "end", None)

        if not start:
            continue

        events.append({
            "id": f"a-{a.pk}",
            "title": f"空き ({getattr(a.coach, 'username', '')})",
            "start": start.isoformat(),
            "end": end.isoformat() if end else None,
            "backgroundColor": "#22c55e",
            "extendedProps": {
                "kind": "availability",
                "availability_id": a.pk,
                "reserve_url": reverse("club:reservation_create") + f"?availability_id={a.pk}"
            }
        })

    for r in reservations:
        start = getattr(r, "start_at", None) or getattr(r, "start", None)
        end = getattr(r, "end_at", None) or getattr(r, "end", None)

        if not start:
            continue

        is_mine = (hasattr(r, "user") and r.user == request.user)

        events.append({
            "id": f"r-{r.pk}",
            "title": "あなたの予約" if is_mine else "予約済み",
            "start": start.isoformat(),
            "end": end.isoformat() if end else None,
            "backgroundColor": "#3b82f6" if is_mine else "#ef4444",
            "extendedProps": {
                "kind": "reservation",
                "is_mine": is_mine,
                "cancel_url": reverse("club:reservation_cancel", args=[r.pk])
            }
        })

    return JsonResponse(events, safe=False)


# =========================
# 予約
# =========================

@login_required
def reservation_create(request):
    availability_id = request.GET.get("availability_id")

    availability = None
    if availability_id:
        try:
            availability = CoachAvailability.objects.get(pk=availability_id)
        except CoachAvailability.DoesNotExist:
            availability = None  # ←ここ重要（404にしない）

    if request.method == "POST":
        form = ReservationForm(request.POST)
        if form.is_valid():
            reservation = form.save(commit=False)

            # ユーザー紐付け
            if hasattr(reservation, "user"):
                reservation.user = request.user

            # availability紐付け（存在する場合のみ）
            if availability and hasattr(reservation, "availability"):
                reservation.availability = availability

            reservation.save()

            # 🔔 仮LINE通知
            print(f"[LINE通知] {request.user} が予約しました")

            messages.success(request, "予約を作成しました")
            return redirect("club:reservation_list")
    else:
        form = ReservationForm()

    return render(request, "reservations/create.html", {
        "form": form,
        "availability": availability
    })


@login_required
def reservation_list(request):
    reservations = Reservation.objects.all().order_by("-id")

    return render(request, "reservations/list.html", {
        "reservations": reservations
    })


@login_required
def reservation_cancel(request, pk):
    reservation = get_object_or_404(Reservation, pk=pk)

    if hasattr(reservation, "status"):
        reservation.status = "cancelled"
        reservation.save()
    else:
        reservation.delete()

    messages.success(request, "予約をキャンセルしました")
    return redirect("club:reservation_list")


# =========================
# コーチ空き
# =========================

@login_required
def coach_availability_list(request):
    availabilities = CoachAvailability.objects.all().order_by("-id")

    return render(request, "coach/availability_list.html", {
        "availabilities": availabilities
    })


@login_required
def coach_availability_create(request):
    form = CoachAvailabilityForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)

        if hasattr(obj, "coach"):
            obj.coach = request.user

        obj.save()

        messages.success(request, "空き時間を登録しました")
        return redirect("club:coach_availability_list")

    return render(request, "coach/availability_create.html", {
        "form": form
    })


@login_required
def coach_availability_delete(request, pk):
    obj = get_object_or_404(CoachAvailability, pk=pk)
    obj.delete()

    messages.success(request, "削除しました")
    return redirect("club:coach_availability_list")


# =========================
# LINE（簡易版）
# =========================

@login_required
def line_link_page(request):
    return render(request, "line_connect.html")
