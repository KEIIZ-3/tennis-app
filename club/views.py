from __future__ import annotations

from datetime import datetime

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .forms import ReservationCreateForm, CoachAvailabilityForm
from .models import Reservation, CoachAvailability, BusinessHours, FacilityClosure, TicketWallet

User = get_user_model()


def _coach_color(coach) -> str:
    """
    coach.color が未設定/デフォルトっぽい時でも、IDから安定した色を割り当てる（保険）。
    adminで色を設定すればそれが優先。
    """
    c = (getattr(coach, "color", "") or "").strip()
    if len(c) == 7 and c.startswith("#"):
        if c.lower() != "#2ecc71":
            return c

    palette = [
        "#2ecc71", "#e67e22", "#9b59b6", "#1abc9c", "#f1c40f",
        "#e84393", "#0984e3", "#6c5ce7", "#00b894", "#d63031",
    ]
    try:
        idx = int(getattr(coach, "id", 0) or 0) % len(palette)
    except Exception:
        idx = 0
    return palette[idx]


@require_GET
def healthz(request):
    return JsonResponse({"ok": True})


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


@login_required
def reservation_create(request):
    day_reservations = None
    wallet, _ = TicketWallet.objects.get_or_create(user=request.user)

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

        try:
            d = datetime.strptime(date_s, "%Y-%m-%d").date() if date_s else None
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
        {"form": form, "day_reservations": day_reservations, "wallet": wallet},
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

    wallet, _ = TicketWallet.objects.get_or_create(user=request.user)

    return render(
        request,
        "reservations/list.html",
        {"reservations": reservations, "tab": tab, "wallet": wallet},
    )


@require_POST
@login_required
def reservation_cancel(request, pk: int):
    r = get_object_or_404(Reservation, pk=pk)

    if r.customer_id != request.user.id:
        raise PermissionDenied

    if r.status == "booked":
        r.status = "cancelled"
        # save() を呼ぶ（models.save で返却処理が走る）
        r.save(update_fields=["status"])
        messages.info(request, "予約をキャンセルしました。")
    else:
        messages.info(request, "この予約は既にキャンセル済みです。")

    nxt = request.POST.get("next") or ""
    if nxt.startswith("/"):
        return redirect(nxt)

    return redirect("club:reservation_list")


@login_required
def coach_availability_list(request):
    if getattr(request.user, "role", "") != "coach":
        raise PermissionDenied

    tab = request.GET.get("tab", "future")
    today = timezone.localdate()

    base_qs = CoachAvailability.objects.filter(coach=request.user).order_by("date", "start_time")

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


@login_required
def calendar_view(request):
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
        {
            "coaches": coaches,
            "selected_coach_id": selected_coach_id,
            "is_coach_user": getattr(request.user, "role", "") == "coach",
        },
    )


@require_GET
@login_required
def calendar_events_api(request):
    coach_id = request.GET.get("coach_id")
    if not coach_id:
        return JsonResponse({"error": "coach_id is required"}, status=400)

    coach = get_object_or_404(User, id=coach_id, role="coach")

    start = request.GET.get("start")
    end = request.GET.get("end")

    def parse_dt(s: str) -> datetime:
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

    events = []
    coach_color = _coach_color(coach)

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

    avail_qs = CoachAvailability.objects.filter(
        coach=coach,
        status="available",
        date__gte=start_date,
        date__lt=end_date,
    ).order_by("date", "start_time")

    for a in avail_qs:
        start_dt = datetime.combine(a.date, a.start_time)
        end_dt = datetime.combine(a.date, a.end_time)

        capacity = int(getattr(a, "capacity", 1) or 1)
        if capacity < 1:
            capacity = 1

        booked = booked_map.get((a.date, a.start_time, a.end_time), 0)
        remaining = max(capacity - booked, 0)

        base_props = {
            "capacity": capacity,
            "booked": booked,
            "remaining": remaining,
            "coachColor": coach_color,
            "coachName": coach.username,
            "date": a.date.isoformat(),
            "start_time": a.start_time.strftime("%H:%M"),
            "end_time": a.end_time.strftime("%H:%M"),
        }

        if remaining <= 0:
            events.append(
                {
                    "title": f"満員 {booked}/{capacity}",
                    "start": start_dt.isoformat(),
                    "end": end_dt.isoformat(),
                    "backgroundColor": "#e74c3c",
                    "borderColor": "#c0392b",
                    "textColor": "#ffffff",
                    "extendedProps": {"kind": "full", **base_props},
                }
            )
        else:
            events.append(
                {
                    "title": f"空き {booked}/{capacity}",
                    "start": start_dt.isoformat(),
                    "end": end_dt.isoformat(),
                    "backgroundColor": coach_color,
                    "borderColor": coach_color,
                    "textColor": "#ffffff",
                    "extendedProps": {
                        "kind": "availability",
                        "reservation_url": (
                            f"/reservations/new/?coach={coach.id}"
                            f"&date={a.date.isoformat()}"
                            f"&start={a.start_time.strftime('%H:%M')}"
                            f"&end={a.end_time.strftime('%H:%M')}"
                        ),
                        **base_props,
                    },
                }
            )

    res_qs = (
        Reservation.objects.filter(
            coach=coach,
            status="booked",
            date__gte=start_date,
            date__lt=end_date,
        )
        .select_related("court", "customer")
        .order_by("date", "start_time")
    )

    for r in res_qs:
        start_dt = datetime.combine(r.date, r.start_time)
        end_dt = datetime.combine(r.date, r.end_time)
        events.append(
            {
                "title": "予約",
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "backgroundColor": "#3498db",
                "borderColor": "#2980b9",
                "textColor": "#ffffff",
                "extendedProps": {
                    "kind": "reservation",
                    "reservation_id": r.id,
                    "coachColor": coach_color,
                    "coachName": coach.username,
                },
            }
        )

    return JsonResponse(events, safe=False)


@require_GET
@login_required
def calendar_event_detail_api(request):
    kind = request.GET.get("kind")
    coach_id = request.GET.get("coach_id")
    if not kind or not coach_id:
        return JsonResponse({"error": "kind and coach_id required"}, status=400)

    coach = get_object_or_404(User, id=coach_id, role="coach")
    is_coach_user = getattr(request.user, "role", "") == "coach" and request.user.id == coach.id
    coach_color = _coach_color(coach)

    if kind == "reservation":
        rid = request.GET.get("reservation_id")
        if not rid:
            return JsonResponse({"error": "reservation_id required"}, status=400)

        r = get_object_or_404(
            Reservation.objects.select_related("court", "customer", "coach"),
            id=rid,
            coach=coach,
            status="booked",
        )

        payload = {
            "kind": "reservation",
            "title": "予約",
            "start": f"{r.date} {r.start_time}",
            "end": f"{r.date} {r.end_time}",
            "coachName": coach.username,
            "coachColor": coach_color,
            "can_cancel": (r.customer_id == request.user.id),
            "cancel_url": f"/reservations/{r.id}/cancel/",
        }

        if is_coach_user:
            payload.update({
                "court": str(r.court),
                "customer": getattr(r.customer, "username", ""),
                "kind_label": getattr(r, "kind", ""),
                "tickets_used": getattr(r, "tickets_used", 0),
            })
        else:
            payload.update({"court": None, "customer": None})

        return JsonResponse(payload)

    if kind in ("availability", "full"):
        date_s = request.GET.get("date")
        st = request.GET.get("start_time")
        et = request.GET.get("end_time")
        if not date_s or not st or not et:
            return JsonResponse({"error": "date/start_time/end_time required"}, status=400)

        booked = Reservation.objects.filter(
            coach=coach, status="booked", date=date_s, start_time=st, end_time=et
        ).count()

        a = CoachAvailability.objects.filter(
            coach=coach, status="available", date=date_s, start_time=st, end_time=et
        ).first()
        capacity = int(getattr(a, "capacity", 1) or 1)
        if capacity < 1:
            capacity = 1

        remaining = max(capacity - booked, 0)

        return JsonResponse({
            "kind": "availability",
            "title": "空き枠" if remaining > 0 else "満員",
            "date": date_s,
            "start_time": st,
            "end_time": et,
            "capacity": capacity,
            "booked": booked,
            "remaining": remaining,
            "coachName": coach.username,
            "coachColor": coach_color,
            "reservation_url": f"/reservations/new/?coach={coach.id}&date={date_s}&start={st}&end={et}",
        })

    return JsonResponse({"error": "unknown kind"}, status=400)


def _is_staff(u):
    return u.is_authenticated and u.is_staff


@user_passes_test(_is_staff)
def manage_reservations(request):
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()

    qs = Reservation.objects.select_related("court", "customer", "coach").order_by("-date", "-start_time")

    if status in ("booked", "cancelled"):
        qs = qs.filter(status=status)

    if q:
        qs = qs.filter(
            Q(customer__username__icontains=q) |
            Q(coach__username__icontains=q) |
            Q(court__name__icontains=q)
        )

    qs = qs[:400]

    return render(request, "admin/reservations_manage.html", {
        "rows": qs, "q": q, "status": status,
    })


@require_POST
@user_passes_test(_is_staff)
def manage_reservation_set_status(request, pk: int):
    r = get_object_or_404(Reservation, pk=pk)
    new_status = request.POST.get("status")
    if new_status not in ("booked", "cancelled"):
        return redirect("club:manage_reservations")

    if r.status != new_status:
        r.status = new_status
        r.save(update_fields=["status"])  # models.saveでチケット返却も走る
        messages.info(request, f"予約ステータスを {new_status} に変更しました。")

    return redirect(request.POST.get("next") or "club:manage_reservations")


@login_required
def business_rules(request):
    """
    ③ 営業時間/休館日（閲覧）
    """
    bhs = BusinessHours.objects.all()
    closures = FacilityClosure.objects.all()[:200]
    return render(request, "business_rules.html", {"bhs": bhs, "closures": closures})
