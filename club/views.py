# club/views.py
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, render, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import ReservationCreateForm
from .models import Reservation


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


# -------- 予約 --------

@login_required
def reservation_create(request):
    if request.method == "POST":
        form = ReservationCreateForm(request.POST, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "予約を作成しました。")
            return redirect("club:reservation_list")
    else:
        form = ReservationCreateForm(user=request.user)

    return render(request, "reservations/create.html", {"form": form})


@login_required
def reservation_list(request):
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
