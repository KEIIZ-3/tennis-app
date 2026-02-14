# club/views.py
import traceback

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponseServerError
from django.shortcuts import get_object_or_404, render, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import ReservationCreateForm, CoachAvailabilityForm
from .models import Reservation, CoachAvailability


def _log_exception(prefix: str):
    # Renderのログ（Live tail）に必ず出る
    print(f"\n=== {prefix} ===")
    print(traceback.format_exc())
    print("=== /exception ===\n")


# -------------------------
# Auth
# -------------------------

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


# -------------------------
# Reservation
# -------------------------

@login_required
def reservation_create(request):
    day_reservations = None
    try:
        if request.method == "POST":
            form = ReservationCreateForm(request.POST, user=request.user)

            date_str = request.POST.get("date")
            if date_str:
                day_reservations = (
                    Reservation.objects.filter(date=date_str, status="booked")
                    .select_related("court", "customer")
                    .order_by("court__name", "start_time")
                )

            if form.is_valid():
                try:
                    form.save()
                except ValidationError as e:
                    form.add_error(None, e.messages)
                else:
                    messages.success(request, "予約を作成しました。")
                    return redirect("club:reservation_list")
        else:
            form = ReservationCreateForm(user=request.user)
            today = timezone.localdate()
            day_reservations = (
                Reservation.objects.filter(date=today, status="booked")
                .select_related("court", "customer")
                .order_by("court__name", "start_time")
            )

        return render(
            request,
            "reservations/create.html",
            {"form": form, "day_reservations": day_reservations},
        )
    except Exception:
        _log_exception("reservation_create crashed")
        return HttpResponseServerError("Server Error (reservation_create). Check Render logs.")


@login_required
def reservation_list(request):
    try:
        tab = request.GET.get("tab", "future")
        today = timezone.localdate()

        base_qs = (
            Reservation.objects.filter(customer=request.user)
            .select_related("court")
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
    except Exception:
        _log_exception("reservation_list crashed")
        return HttpResponseServerError("Server Error (reservation_list). Check Render logs.")


@require_POST
@login_required
def reservation_cancel(request, pk: int):
    try:
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
    except Exception:
        _log_exception("reservation_cancel crashed")
        return HttpResponseServerError("Server Error (reservation_cancel). Check Render logs.")


# -------------------------
# Coach Availability
# -------------------------

def _require_coach(user):
    return getattr(user, "role", None) == "coach"


@login_required
def coach_availability_list(request):
    try:
        if not _require_coach(request.user):
            raise PermissionDenied

        tab = request.GET.get("tab", "future")
        today = timezone.localdate()

        base_qs = CoachAvailability.objects.filter(coach=request.user).order_by("date", "start_time")

        if tab == "past":
            items = base_qs.filter(date__lt=today).order_by("-date", "-start_time")
        else:
            items = base_qs.filter(date__gte=today)

        return render(request, "coach/availability_list.html", {"items": items, "tab": tab})
    except Exception:
        _log_exception("coach_availability_list crashed")
        return HttpResponseServerError("Server Error (coach_availability_list). Check Render logs.")


@login_required
def coach_availability_create(request):
    try:
        if not _require_coach(request.user):
            raise PermissionDenied

        if request.method == "POST":
            form = CoachAvailabilityForm(request.POST, coach=request.user)
            if form.is_valid():
                try:
                    form.save()
                except ValidationError as e:
                    form.add_error(None, e.messages)
                else:
                    messages.success(request, "空き時間を登録しました。")
                    return redirect("club:coach_availability_list")
        else:
            form = CoachAvailabilityForm(coach=request.user)

        return render(request, "coach/availability_create.html", {"form": form})
    except Exception:
        _log_exception("coach_availability_create crashed")
        return HttpResponseServerError("Server Error (coach_availability_create). Check Render logs.")


@require_POST
@login_required
def coach_availability_delete(request, pk: int):
    try:
        if not _require_coach(request.user):
            raise PermissionDenied

        item = get_object_or_404(CoachAvailability, pk=pk, coach=request.user)
        item.delete()
        messages.info(request, "空き時間を削除しました。")
        return redirect("club:coach_availability_list")
    except Exception:
        _log_exception("coach_availability_delete crashed")
        return HttpResponseServerError("Server Error (coach_availability_delete). Check Render logs.")

