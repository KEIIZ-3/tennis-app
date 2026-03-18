from __future__ import annotations

from datetime import datetime

from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .forms import CoachAvailabilityForm, ReservationCreateForm
from .models import (
    BusinessHours,
    CoachAvailability,
    FacilityClosure,
    Reservation,
    TicketWallet,
)
from .services.notifications import (
    send_reservation_cancelled_notifications,
    send_reservation_created_notifications,
)

User = get_user_model()


def _coach_color(coach) -> str:
    c = (getattr(coach, "color", "") or "").strip()
    if len(c) == 7 and c.startswith("#"):
        return c

    palette = [
        "#2ecc71",
        "#e67e22",
        "#9b59b6",
        "#1abc9c",
        "#f1c40f",
        "#e84393",
        "#0984e3",
        "#6c5ce7",
        "#00b894",
        "#d63031",
    ]
    try:
        idx = int(getattr(coach, "id", 0) or 0) % len(palette)
    except Exception:
        idx = 0
    return palette[idx]


def _is_coach(user) -> bool:
    return user.is_authenticated and getattr(user, "role", "") == "coach"


def _is_staff(user) -> bool:
    return user.is_authenticated and user.is_staff


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
            reservation = form.save()
            send_reservation_created_notifications(reservation)
            messages.success(request, "予約を作成しました。")
            return redirect("club:reservation_list")

        try:
            selected_date = form.cleaned_data.get("date")
        except Exception:
            selected_date = None
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
            selected_date = datetime.strptime(date_s, "%Y-%m-%d").date() if date_s else None
        except Exception:
            selected_date = None

    if selected_date:
        day_reservations = (
            Reservation.objects.filter(date=selected_date, status="booked")
            .select_related("court", "customer", "coach")
            .order_by("start_time", "court__name")
        )

    return render(
        request,
        "reservations/create.html",
        {
            "form": form,
            "day_reservations": day_reservations,
            "wallet": wallet,
        },
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
    elif tab == "cancelled":
        reservations = base_qs.filter(status="cancelled").order_by("-date", "-start_time")
    else:
        reservations = base_qs.filter(status="booked", date__gte=today)

    wallet, _ = TicketWallet.objects.get_or_create(user=request.user)

    return render(
        request,
        "reservations/list.html",
        {
            "reservations": reservations,
            "tab": tab,
            "wallet": wallet,
        },
    )


@require_POST
@login_required
def reservation_cancel(request, pk: int):
    reservation = get_object_or_404(Reservation, pk=pk)

    if reservation.customer_id != request.user.id:
        raise PermissionDenied

    if reservation.status != "booked":
        messages.info(request, "この予約は既にキャンセル済みです。")
    elif not reservation.can_cancel_now:
        messages.error(request, "キャンセル期限を過ぎているため、この予約はキャンセルできません。")
    else:
        reservation.status = "cancelled"
        reservation.save(update_fields=["status"])
        send_reservation_cancelled_notifications(reservation)
        messages.info(request, "予約をキャンセルしました。")

    nxt = request.POST.get("next") or ""
    if nxt.startswith("/"):
        return redirect(nxt)

    return redirect("club:reservation_list")


@login_required
def coach_availability_list(request):
    if not _is_coach(request.user):
        raise PermissionDenied

    tab = request.GET.get("tab", "future")
    today = timezone.localdate()

    base_qs = (
        CoachAvailability.objects.filter(coach=request.user)
        .order_by("date", "start_time")
    )

    if tab == "past":
        items = base_qs.filter(date__lt=today).order_by("-date", "-start_time")
    else:
        items = base_qs.filter(date__gte=today)

    return render(
        request,
        "coach/availability_list.html",
        {
            "items": items,
            "tab": tab,
        },
    )


@login_required
def coach_availability_create(request):
    if not _is_coach(request.user):
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
    if not _is_coach(request.user):
        raise PermissionDenied

    availability = get_object_or_404(CoachAvailability, pk=pk, coach=request.user)
    availability.delete()
    messages.info(request, "空き時間を削除しました。")
    return redirect("club:coach_availability_list")


@login_required
def calendar_view(request):
    coaches = User.objects.filter(role="coach", is_active=True).order_by("username")

    selected_coach_id = request.GET.get("coach")
    if _is_coach(request.user):
        selected_coach_id = str(request.user.id)
    elif not selected_coach_id and coaches.exists():
        selected_coach_id = str(coaches.first().id)

    return render(
        request,
        "calendar.html",
        {
            "coaches": coaches,
            "selected_coach_id": selected_coach_id,
            "is_coach_user": _is_coach(request.user),
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

    def parse_dt(value: str) -> datetime:
        if "T" in value:
            value = value.split("T", 1)[0]
        return datetime.strptime(value, "%Y-%m-%d")

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

    avail_qs = (
        CoachAvailability.objects.filter(
            coach=coach,
            status="available",
            date__gte=start_date,
            date__lt=end_date,
        )
        .order_by("date", "start_time")
    )

    for slot in avail_qs:
        start_dt = datetime.combine(slot.date, slot.start_time)
        end_dt = datetime.combine(slot.date, slot.end_time)

        capacity = int(getattr(slot, "capacity", 1) or 1)
        booked = booked_map.get((slot.date, slot.start_time, slot.end_time), 0)
        remaining = max(capacity - booked, 0)

        base_props = {
            "capacity": capacity,
            "booked": booked,
            "remaining": remaining,
            "coachColor": coach_color,
            "coachName": coach.username,
            "date": slot.date.isoformat(),
            "start_time": slot.start_time.strftime("%H:%M"),
            "end_time": slot.end_time.strftime("%H:%M"),
        }

        if remaining <= 0:
            events.append(
                {
                    "title": f"満員 {booked}/{capacity}",
                    "start": start_dt.isoformat(),
                    "end": end_dt.isoformat(),
                    "display": "block",
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
                    "display": "block",
                    "backgroundColor": coach_color,
                    "borderColor": coach_color,
                    "textColor": "#ffffff",
                    "extendedProps": {
                        "kind": "availability",
                        "reservation_url": (
                            f"/reservations/new/?coach={coach.id}"
                            f"&date={slot.date.isoformat()}"
                            f"&start={slot.start_time.strftime('%H:%M')}"
                            f"&end={slot.end_time.strftime('%H:%M')}"
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
        .select_related("court", "customer", "coach")
        .order_by("date", "start_time")
    )

    for reservation in res_qs:
        start_dt = datetime.combine(reservation.date, reservation.start_time)
        end_dt = datetime.combine(reservation.date, reservation.end_time)

        is_mine = reservation.customer_id == request.user.id
        title = "自分の予約" if is_mine else "予約"

        if reservation.kind == "group_lesson":
            bg = "#8e44ad" if is_mine else "#9b59b6"
            border = "#6c3483"
        elif reservation.kind == "court_rental":
            bg = "#16a085" if is_mine else "#1abc9c"
            border = "#117a65"
        else:
            bg = "#2980b9" if is_mine else "#3498db"
            border = "#1f618d"

        events.append(
            {
                "title": title,
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "display": "block",
                "backgroundColor": bg,
                "borderColor": border,
                "textColor": "#ffffff",
                "extendedProps": {
                    "kind": "reservation",
                    "reservation_id": reservation.id,
                    "coachColor": coach_color,
                    "coachName": coach.username,
                    "is_mine": is_mine,
                    "kind_label": reservation.get_kind_display(),
                    "court_name": str(reservation.court),
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
    is_coach_user = _is_coach(request.user) and request.user.id == coach.id
    coach_color = _coach_color(coach)

    if kind == "reservation":
        reservation_id = request.GET.get("reservation_id")
        if not reservation_id:
            return JsonResponse({"error": "reservation_id required"}, status=400)

        reservation = get_object_or_404(
            Reservation.objects.select_related("court", "customer", "coach"),
            id=reservation_id,
            coach=coach,
            status="booked",
        )

        payload = {
            "kind": "reservation",
            "title": "予約",
            "start": f"{reservation.date} {reservation.start_time}",
            "end": f"{reservation.date} {reservation.end_time}",
            "coachName": coach.username,
            "coachColor": coach_color,
            "can_cancel": reservation.customer_id == request.user.id and reservation.can_cancel_now,
            "cancel_url": f"/reservations/{reservation.id}/cancel/",
            "is_mine": reservation.customer_id == request.user.id,
            "kind_label": reservation.get_kind_display(),
        }

        if is_coach_user or reservation.customer_id == request.user.id:
            payload.update(
                {
                    "court": str(reservation.court),
                    "customer": getattr(reservation.customer, "username", ""),
                    "tickets_used": getattr(reservation, "tickets_used", 0),
                    "note": reservation.note,
                }
            )
        else:
            payload.update(
                {
                    "court": str(reservation.court),
                    "customer": None,
                    "tickets_used": None,
                    "note": "",
                }
            )

        return JsonResponse(payload)

    if kind in ("availability", "full"):
        date_s = request.GET.get("date")
        start_time = request.GET.get("start_time")
        end_time = request.GET.get("end_time")

        if not date_s or not start_time or not end_time:
            return JsonResponse({"error": "date/start_time/end_time required"}, status=400)

        booked = Reservation.objects.filter(
            coach=coach,
            status="booked",
            date=date_s,
            start_time=start_time,
            end_time=end_time,
        ).count()

        availability = CoachAvailability.objects.filter(
            coach=coach,
            status="available",
            date=date_s,
            start_time=start_time,
            end_time=end_time,
        ).first()

        capacity = int(getattr(availability, "capacity", 1) or 1)
        remaining = max(capacity - booked, 0)

        return JsonResponse(
            {
                "kind": "availability",
                "title": "空き枠" if remaining > 0 else "満員",
                "date": date_s,
                "start_time": start_time,
                "end_time": end_time,
                "capacity": capacity,
                "booked": booked,
                "remaining": remaining,
                "coachName": coach.username,
                "coachColor": coach_color,
                "reservation_url": (
                    f"/reservations/new/?coach={coach.id}"
                    f"&date={date_s}&start={start_time}&end={end_time}"
                ),
            }
        )

    return JsonResponse({"error": "unknown kind"}, status=400)


@user_passes_test(_is_staff)
def manage_reservations(request):
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()
    kind = (request.GET.get("kind") or "").strip()

    qs = (
        Reservation.objects.select_related("court", "customer", "coach")
        .order_by("-date", "-start_time")
    )

    if status in ("booked", "cancelled"):
        qs = qs.filter(status=status)

    if kind in ("private_lesson", "group_lesson", "court_rental"):
        qs = qs.filter(kind=kind)

    if q:
        qs = qs.filter(
            Q(customer__username__icontains=q)
            | Q(customer__email__icontains=q)
            | Q(coach__username__icontains=q)
            | Q(coach__email__icontains=q)
            | Q(court__name__icontains=q)
            | Q(note__icontains=q)
        )

    qs = qs[:400]

    return render(
        request,
        "admin/reservations_manage.html",
        {
            "rows": qs,
            "q": q,
            "status": status,
            "kind": kind,
        },
    )


@require_POST
@user_passes_test(_is_staff)
def manage_reservation_set_status(request, pk: int):
    reservation = get_object_or_404(Reservation, pk=pk)
    new_status = request.POST.get("status")

    if new_status not in ("booked", "cancelled"):
        return redirect("club:manage_reservations")

    if reservation.status != new_status:
        reservation.status = new_status
        reservation.save(update_fields=["status"])

        if new_status == "cancelled":
            send_reservation_cancelled_notifications(reservation)

        messages.info(request, f"予約ステータスを {new_status} に変更しました。")

    return redirect(request.POST.get("next") or "club:manage_reservations")


@login_required
def business_rules(request):
    bhs = BusinessHours.objects.all()
    closures = FacilityClosure.objects.all()[:200]
    return render(
        request,
        "business_rules.html",
        {
            "bhs": bhs,
            "closures": closures,
        },
    )
