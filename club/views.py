from __future__ import annotations

from datetime import datetime

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .forms import ReservationCreateForm, CoachAvailabilityForm
from .models import Reservation, CoachAvailability

User = get_user_model()


# -----------------------------
# Health check（ログイン不要）
# -----------------------------
def healthz(request):
    return HttpResponse("ok")


# -----------------------------
# Auth / Home
# -----------------------------
def login_view(request):
    if request.user.is_authenticated:
        return redirect("club:home")

    if request.method == "POST":
        username = request.POST.get("username", "")
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user:
            login(request, user)
            return redirect("club:home")
        return render(request, "login.html", {"error": "ログインに失敗しました"})

    return render(request, "login.html")


def logout_view(request):
    logout(request)
    return redirect("club:login")


@login_required
def home(request):
    return render(request, "home.html")


# -----------------------------
# 予約
# -----------------------------
@login_required
def reservation_create(request):
    """
    GET:
      - カレンダーから ?coach=&date=&start=&end= が来たら初期値に反映
      - 可能ならその日の予約も表示
    POST:
      - 予約作成
      - 失敗時もその日の予約は表示
    """
    day_reservations = None

    if request.method == "POST":
        form = ReservationCreateForm(request.POST, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "予約を作成しました。")
            return redirect("club:reservation_list")

        try:
            d = form.cleaned_data.get("date")
        except Exception:
            d = None

        if d:
            day_reservations = (
                Reservation.objects.filter(date=d, status="booked")
                .select_related("court", "customer", "coach")
                .order_by("start_time")
            )

    else:
        # ✅ カレンダーからの遷移パラメータを初期値として反映
        initial = {}
        coach = request.GET.get("coach")
        date_s = request.GET.get("date")
        start = request.GET.get("start")
        end = request.GET.get("end")

        if coach:
            initial["coach"] = coach
        if date_s:
            initial["date"] = date_s
        if start:
            initial["start_time"] = start
        if end:
            initial["end_time"] = end

        form = ReservationCreateForm(user=request.user, initial=initial)

        # 初期値があるなら、その日の予約も表示
        try:
            if date_s:
                d = datetime.strptime(date_s, "%Y-%m-%d").date()
            else:
                d = None
        except Exception:
            d = None

        if d:
            day_reservations = (
                Reservation.objects.filter(date=d, status="booked")
                .select_related("court", "customer", "coach")
                .order_by("start_time")
            )

    return render(
        request,
        "reservations/create.html",
        {"form": form, "day_reservations": day_reservations},
    )


@login_required
def reservation_list(request):
    tab = request.GET.get("tab", "future")
    today = timezone.localdate()

    base_qs = (
        Reservation.objects.filter(customer=request.user)
        .select_related("court", "coach")
        .order_by("date", "start_time")
    )

    if tab == "past":
        reservations = base_qs.filter(date__lt=today).order_by("-date", "-start_time")
    else:
        reservations = base_qs.filter(date__gte=today)

    return render(
        request,
        "reservations/list.html",
        {"reservations": reservations, "tab": tab},
    )


@require_POST
@login_required
def reservation_cancel(request, pk: int):
    r = get_object_or_404(Reservation, pk=pk)

    if r.customer_id != request.user.id:
        raise PermissionDenied

    if r.status == "booked":
        r.status = "cancelled"
        r.save(update_fields=["status"])
        messages.info(request, "予約をキャンセルしました。")
    else:
        messages.info(request, "この予約は既にキャンセル済みです。")

    return redirect("club:reservation_list")


# -----------------------------
# コーチ空き時間
# -----------------------------
@login_required
def coach_availability_list(request):
    if getattr(request.user, "role", "") != "coach":
        raise PermissionDenied

    tab = request.GET.get("tab", "future")
    today = timezone.localdate()

    base_qs = CoachAvailability.objects.filter(coach=request.user).order_by(
        "date", "start_time"
    )

    if tab == "past":
        items = base_qs.filter(date__lt=today).order_by("-date", "-start_time")
    else:
        items = base_qs.filter(date__gte=today)

    return render(request, "coach/availability_list.html", {"items": items, "tab": tab})


@login_required
def coach_availability_create(request):
    if getattr(request.user, "role", "") != "coach":
        raise PermissionDenied

    if request.method == "POST":
        form = CoachAvailabilityForm(request.POST, coach=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "空き時間を登録しました。")
            return redirect("club:coach_availability_list")
    else:
        form = CoachAvailabilityForm(coach=request.user)

    return render(request, "coach/availability_create.html", {"form": form})


@require_POST
@login_required
def coach_availability_delete(request, pk: int):
    if getattr(request.user, "role", "") != "coach":
        raise PermissionDenied

    a = get_object_or_404(CoachAvailability, pk=pk, coach=request.user)
    a.delete()
    messages.info(request, "空き時間を削除しました。")
    return redirect("club:coach_availability_list")


# -----------------------------
# カレンダー
# -----------------------------
@login_required
def calendar_view(request):
    """
    - 顧客：コーチを選んで「空き/満員」と「予約」を確認
    - コーチ：自分の予定（空き/満員＋予約）を確認（デフォルト自分）
    """
    coaches = User.objects.filter(role="coach", is_active=True).order_by("username")

    selected_coach_id = request.GET.get("coach")
    if getattr(request.user, "role", "") == "coach":
        selected_coach_id = str(request.user.id)
    else:
        if not selected_coach_id and coaches.exists():
            selected_coach_id = str(coaches.first().id)

    return render(
        request,
        "calendar.html",
        {"coaches": coaches, "selected_coach_id": selected_coach_id},
    )


@require_GET
@login_required
def calendar_events_api(request):
    """
    FullCalendar用イベントAPI（枠ごとのcapacity対応）

    - CoachAvailability（available）を枠イベントとして返す
      - booked/capacity をタイトルに表示
      - booked >= capacity なら「満員」赤（reservation_url無し＝クリック不可）
      - それ以外は「空き」緑（reservation_url有り）
    - Reservation（booked）も個別イベントとして返す（ブルー）
    """
    from django.db.models import Count

    coach_id = request.GET.get("coach_id")
    if not coach_id:
        return JsonResponse({"error": "coach_id is required"}, status=400)

    coach = get_object_or_404(User, id=coach_id, role="coach")

    start = request.GET.get("start")
    end = request.GET.get("end")

    def parse_dt(s: str) -> datetime:
        # "2026-02-15" or "2026-02-15T00:00:00+09:00"
        if "T" in s:
            s = s.split("T", 1)[0]
        return datetime.strptime(s, "%Y-%m-%d")

    if start and end:
        start_date = parse_dt(start).date()
        end_date = parse_dt(end).date()
    else:
        today = timezone.localdate()
        start_date = today.replace(day=1)
        end_date = today.replace(day=28)

    events: list[dict] = []

    # 枠単位で予約数を集計（date + start_time + end_time）
    booked_counts_qs = (
        Reservation.objects.filter(
            coach=coach,
            status="booked",
            date__gte=start_date,
            date__lt=end_date,
        )
        .values("date", "start_time", "end_time")
        .annotate(booked=Count("id"))
    )
    booked_map = {
        (row["date"], row["start_time"], row["end_time"]): row["booked"]
        for row in booked_counts_qs
    }

    # 1) 枠（空き or 満員）
    avail_qs = CoachAvailability.objects.filter(
        coach=coach,
        status="available",
        date__gte=start_date,
        date__lt=end_date,
    ).order_by("date", "start_time")

    for a in avail_qs:
        start_dt = datetime.combine(a.date, a.start_time)
        end_dt = datetime.combine(a.date, a.end_time)

        try:
            capacity = int(getattr(a, "capacity", 1) or 1)
        except Exception:
            capacity = 1
        if capacity < 1:
            capacity = 1

        booked = booked_map.get((a.date, a.start_time, a.end_time), 0)
        remaining = max(capacity - booked, 0)

        if remaining <= 0:
            # 満員（赤）→ 予約URLなし（クリックしても遷移させない）
            events.append(
                {
                    "title": f"満員 {booked}/{capacity}",
                    "start": start_dt.isoformat(),
                    "end": end_dt.isoformat(),
                    "backgroundColor": "#e74c3c",
                    "borderColor": "#c0392b",
                    "textColor": "#ffffff",
                    "extendedProps": {
                        "kind": "full",
                        "capacity": capacity,
                        "booked": booked,
                        "remaining": remaining,
                    },
                }
            )
        else:
            # 空き（緑）→ 予約URLあり
            events.append(
                {
                    "title": f"空き {booked}/{capacity}",
                    "start": start_dt.isoformat(),
                    "end": end_dt.isoformat(),
                    "backgroundColor": "#2ecc71",
                    "borderColor": "#27ae60",
                    "textColor": "#ffffff",
                    "extendedProps": {
                        "kind": "availability",
                        "capacity": capacity,
                        "booked": booked,
                        "remaining": remaining,
                        "reservation_url": (
                            f"/reservations/new/?coach={coach.id}"
                            f"&date={a.date.isoformat()}"
                            f"&start={a.start_time.strftime('%H:%M')}"
                            f"&end={a.end_time.strftime('%H:%M')}"
                        ),
                    },
                }
            )

    # 2) 予約（ブルー）
    res_qs = (
        Reservation.objects.filter(
            coach=coach,
            status="booked",
            date__gte=start_date,
            date__lt=end_date,
        )
        .select_related("court")
        .order_by("date", "start_time")
    )

    for r in res_qs:
        start_dt = datetime.combine(r.date, r.start_time)
        end_dt = datetime.combine(r.date, r.end_time)
        events.append(
            {
                "title": f"予約（{r.court}）",
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "backgroundColor": "#3498db",
                "borderColor": "#2980b9",
                "textColor": "#ffffff",
                "extendedProps": {"kind": "reservation"},
            }
        )

    return JsonResponse(events, safe=False)
