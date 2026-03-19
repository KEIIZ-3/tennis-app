from datetime import datetime, timedelta, time

from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import select_template
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from . import forms as club_forms
from . import models as club_models
from .notifications import (
    notify_user,
    build_reservation_created_message,
    build_reservation_canceled_message,
)


User = get_user_model()

Reservation = getattr(club_models, "Reservation", None)
CoachAvailability = getattr(club_models, "CoachAvailability", None)
LineAccountLink = getattr(club_models, "LineAccountLink", None)

ReservationForm = getattr(club_forms, "ReservationForm", None)
CoachAvailabilityForm = getattr(club_forms, "CoachAvailabilityForm", None)
LineAccountLinkForm = getattr(club_forms, "LineAccountLinkForm", None)


def _pick_template(*template_names):
    return select_template(template_names).template.name


def _first_attr(obj, names, default=None):
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def _normalize_dt(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if timezone.is_naive(value):
            return timezone.make_aware(value, timezone.get_current_timezone())
        return value
    return None


def _combine_date_time(date_value, time_value):
    if date_value is None:
        return None

    if time_value is None:
        time_value = time(0, 0)

    dt = datetime.combine(date_value, time_value)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _extract_start_end(obj):
    start_dt = _normalize_dt(
        _first_attr(
            obj,
            [
                "start",
                "start_at",
                "starts_at",
                "start_datetime",
                "start_dt",
            ],
        )
    )
    end_dt = _normalize_dt(
        _first_attr(
            obj,
            [
                "end",
                "end_at",
                "ends_at",
                "end_datetime",
                "end_dt",
            ],
        )
    )

    if start_dt is None:
        date_value = _first_attr(obj, ["date", "day"])
        start_time_value = _first_attr(obj, ["start_time", "from_time", "time"])
        start_dt = _combine_date_time(date_value, start_time_value)

    if end_dt is None:
        date_value = _first_attr(obj, ["date", "day"])
        end_time_value = _first_attr(obj, ["end_time", "to_time"])
        if date_value is not None and end_time_value is not None:
            end_dt = _combine_date_time(date_value, end_time_value)

    if start_dt is not None and end_dt is None:
        end_dt = start_dt + timedelta(hours=1)

    return start_dt, end_dt


def _get_user_role(user):
    return getattr(user, "role", None)


def _is_coach(user):
    if not user.is_authenticated:
        return False
    if user.is_superuser or user.is_staff:
        return True
    return _get_user_role(user) == "coach"


def _get_coaches():
    if hasattr(User, "role"):
        return User.objects.filter(role="coach").order_by("id")
    return User.objects.filter(is_staff=True).order_by("id")


def _get_object_coach_id(obj):
    coach_obj = _first_attr(obj, ["coach"])
    if coach_obj is not None and hasattr(coach_obj, "pk"):
        return coach_obj.pk

    coach_id = _first_attr(obj, ["coach_id"])
    if coach_id is not None:
        return coach_id

    return None


def _get_object_coach_name(obj):
    coach_obj = _first_attr(obj, ["coach"])
    if coach_obj is not None:
        if hasattr(coach_obj, "get_full_name"):
            full_name = coach_obj.get_full_name() or ""
            if full_name:
                return full_name
        return getattr(coach_obj, "username", str(coach_obj))
    return ""


def _get_reservation_owner(obj):
    return _first_attr(obj, ["user", "member", "customer", "created_by"])


def _filter_by_coach(iterable, coach_id):
    if not coach_id:
        return list(iterable)

    result = []
    for obj in iterable:
        obj_coach_id = _get_object_coach_id(obj)
        if str(obj_coach_id) == str(coach_id):
            result.append(obj)
    return result


def _safe_queryset_all(model_class):
    if model_class is None:
        return []
    try:
        return list(model_class.objects.all())
    except Exception:
        return []


def _build_reserve_url_from_availability(obj):
    url = reverse("club:reservation_create")
    availability_id = getattr(obj, "pk", None)
    if availability_id:
        return f"{url}?availability_id={availability_id}"
    return url


def _home_template_name():
    return _pick_template(
        "home.html",
        "club/home.html",
    )


def _login_template_name():
    return _pick_template(
        "registration/login.html",
        "login.html",
        "club/login.html",
    )


def _reservation_create_template_name():
    return _pick_template(
        "reservations/create.html",
        "reservations/reservation_create.html",
        "reservation_create.html",
        "club/reservation_create.html",
    )


def _reservation_list_template_name():
    return _pick_template(
        "reservations/list.html",
        "reservations/reservation_list.html",
        "reservation_list.html",
        "club/reservation_list.html",
    )


def _coach_availability_list_template_name():
    return _pick_template(
        "coach/availability_list.html",
        "availability_list.html",
        "club/availability_list.html",
    )


def _coach_availability_create_template_name():
    return _pick_template(
        "coach/availability_create.html",
        "availability_create.html",
        "club/availability_create.html",
    )


def _line_link_template_name():
    return _pick_template(
        "line_connect.html",
        "line/link.html",
        "line/link_page.html",
        "line_link.html",
        "club/line_link.html",
    )


def _reservation_cancel_template_name():
    return _pick_template(
        "reservations/cancel.html",
        "reservations/reservation_cancel.html",
        "reservation_cancel.html",
        "club/reservation_cancel.html",
    )


def _coach_availability_delete_template_name():
    return _pick_template(
        "coach/availability_delete.html",
        "availability_delete.html",
        "club/availability_delete.html",
    )


def login_view(request):
    if request.user.is_authenticated:
        return redirect("club:home")

    form = AuthenticationForm(request, data=request.POST or None)

    if request.method == "POST" and form.is_valid():
        login(request, form.get_user())
        next_url = request.GET.get("next") or request.POST.get("next")
        if next_url:
            return redirect(next_url)
        return redirect("club:home")

    return render(
        request,
        _login_template_name(),
        {
            "form": form,
            "next": request.GET.get("next", ""),
        },
    )


def logout_view(request):
    logout(request)
    return redirect("club:login")


@login_required
def home(request):
    coaches = _get_coaches()
    selected_coach = request.GET.get("coach") or request.GET.get("coach_id") or ""

    context = {
        "coaches": coaches,
        "selected_coach": str(selected_coach),
        "line_link_url": reverse("club:line_link"),
        "calendar_events_url": reverse("club:calendar_events"),
        "calendar_events_legacy_url": reverse("club:calendar_events_legacy"),
    }
    return render(request, _home_template_name(), context)


def healthz(request):
    return HttpResponse("ok")


@require_GET
@login_required
def calendar_events(request):
    coach_id = request.GET.get("coach_id") or request.GET.get("coach")

    availability_objects = _filter_by_coach(_safe_queryset_all(CoachAvailability), coach_id)
    reservation_objects = _filter_by_coach(_safe_queryset_all(Reservation), coach_id)

    events = []

    for obj in availability_objects:
        start_dt, end_dt = _extract_start_end(obj)
        if start_dt is None:
            continue

        obj_coach_id = _get_object_coach_id(obj)
        coach_name = _get_object_coach_name(obj)

        events.append(
            {
                "id": f"availability-{getattr(obj, 'pk', id(obj))}",
                "title": "空き" if not coach_name else f"空き（{coach_name}）",
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat() if end_dt else None,
                "backgroundColor": "#22c55e",
                "borderColor": "#22c55e",
                "textColor": "#ffffff",
                "display": "block",
                "extendedProps": {
                    "kind": "availability",
                    "coach_id": obj_coach_id,
                    "coach_name": coach_name,
                    "reserve_url": _build_reserve_url_from_availability(obj),
                },
            }
        )

    for obj in reservation_objects:
        start_dt, end_dt = _extract_start_end(obj)
        if start_dt is None:
            continue

        owner = _get_reservation_owner(obj)
        is_mine = bool(owner and request.user.is_authenticated and getattr(owner, "pk", None) == request.user.pk)

        title = "あなたの予約" if is_mine else "予約済み"
        bg = "#3b82f6" if is_mine else "#ef4444"

        cancel_url = None
        if is_mine:
            try:
                cancel_url = reverse("club:reservation_cancel", kwargs={"pk": obj.pk})
            except Exception:
                cancel_url = None

        events.append(
            {
                "id": f"reservation-{getattr(obj, 'pk', id(obj))}",
                "title": title,
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat() if end_dt else None,
                "backgroundColor": bg,
                "borderColor": bg,
                "textColor": "#ffffff",
                "display": "block",
                "extendedProps": {
                    "kind": "reservation",
                    "coach_id": _get_object_coach_id(obj),
                    "coach_name": _get_object_coach_name(obj),
                    "is_mine": is_mine,
                    "cancel_url": cancel_url,
                },
            }
        )

    return JsonResponse(events, safe=False)


@login_required
def reservation_create(request):
    if Reservation is None:
        raise Http404("Reservation model is not available.")

    availability = None
    availability_id = request.GET.get("availability_id")

    if availability_id and CoachAvailability is not None:
        try:
            availability = CoachAvailability.objects.filter(pk=availability_id).first()
        except Exception:
            availability = None

    if ReservationForm is None:
        raise Http404("Reservation form is not available.")

    initial = {}
    if availability is not None:
        if hasattr(availability, "coach_id") and "coach" in getattr(ReservationForm, "base_fields", {}):
            initial["coach"] = availability.coach_id

        start_dt, end_dt = _extract_start_end(availability)
        if start_dt:
            if "start_at" in getattr(ReservationForm, "base_fields", {}):
                initial["start_at"] = start_dt
            elif "start" in getattr(ReservationForm, "base_fields", {}):
                initial["start"] = start_dt
            elif "date" in getattr(ReservationForm, "base_fields", {}):
                initial["date"] = start_dt.date()
                if "start_time" in getattr(ReservationForm, "base_fields", {}):
                    initial["start_time"] = start_dt.time().replace(second=0, microsecond=0)
                if end_dt and "end_time" in getattr(ReservationForm, "base_fields", {}):
                    initial["end_time"] = end_dt.time().replace(second=0, microsecond=0)

        if hasattr(availability, "court_id") and "court" in getattr(ReservationForm, "base_fields", {}):
            initial["court"] = availability.court_id

    form = ReservationForm(request.POST or None, initial=initial)

    if request.method == "POST" and form.is_valid():
        reservation = form.save(commit=False)

        if hasattr(reservation, "user_id") and not getattr(reservation, "user_id", None):
            reservation.user = request.user
        elif hasattr(reservation, "member_id") and not getattr(reservation, "member_id", None):
            reservation.member = request.user
        elif hasattr(reservation, "customer_id") and not getattr(reservation, "customer_id", None):
            reservation.customer = request.user
        elif hasattr(reservation, "created_by_id") and not getattr(reservation, "created_by_id", None):
            reservation.created_by = request.user

        if availability is not None and hasattr(reservation, "availability_id") and not getattr(reservation, "availability_id", None):
            reservation.availability = availability

        reservation.save()

        try:
            subject, message = build_reservation_created_message(reservation)
            notify_user(request.user, subject, message)
        except Exception:
            pass

        messages.success(request, "予約を作成しました。")
        return redirect("club:reservation_list")

    return render(
        request,
        _reservation_create_template_name(),
        {
            "form": form,
            "availability": availability,
        },
    )


@login_required
def reservation_list(request):
    if Reservation is None:
        raise Http404("Reservation feature is not available.")

    qs = Reservation.objects.all().order_by("-id")

    if not (request.user.is_superuser or request.user.is_staff or _is_coach(request.user)):
        if hasattr(Reservation, "user"):
            qs = qs.filter(user=request.user)
        elif hasattr(Reservation, "member"):
            qs = qs.filter(member=request.user)
        elif hasattr(Reservation, "customer"):
            qs = qs.filter(customer=request.user)
        elif hasattr(Reservation, "created_by"):
            qs = qs.filter(created_by=request.user)

    return render(
        request,
        _reservation_list_template_name(),
        {
            "reservations": qs,
        },
    )


@login_required
def reservation_cancel(request, pk):
    if Reservation is None:
        raise Http404("Reservation feature is not available.")

    reservation = get_object_or_404(Reservation, pk=pk)

    owner = _get_reservation_owner(reservation)
    can_manage = (
        request.user.is_superuser
        or request.user.is_staff
        or _is_coach(request.user)
        or (owner is not None and getattr(owner, "pk", None) == request.user.pk)
    )

    if not can_manage:
        raise Http404()

    try:
        subject, message = build_reservation_canceled_message(reservation)
    except Exception:
        subject, message = None, None

    if hasattr(reservation, "status"):
        reservation.status = "cancelled"
        reservation.save(update_fields=["status"])
    else:
        reservation.delete()

    if subject and message:
        try:
            notify_user(request.user, subject, message)
        except Exception:
            pass

    messages.success(request, "予約をキャンセルしました。")
    return redirect("club:reservation_list")


@login_required
def coach_availability_list(request):
    if CoachAvailability is None:
        raise Http404("Coach availability feature is not available.")

    qs = CoachAvailability.objects.all().order_by("-id")

    if _is_coach(request.user) and not (request.user.is_superuser or request.user.is_staff):
        if hasattr(CoachAvailability, "coach"):
            qs = qs.filter(coach=request.user)

    return render(
        request,
        _coach_availability_list_template_name(),
        {
            "availabilities": qs,
        },
    )


@login_required
def coach_availability_create(request):
    if CoachAvailability is None:
        raise Http404("Coach availability feature is not available.")

    if CoachAvailabilityForm is None:
        raise Http404("Coach availability form is not available.")

    if not _is_coach(request.user):
        raise Http404()

    form = CoachAvailabilityForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        availability = form.save(commit=False)

        if hasattr(availability, "coach_id") and not getattr(availability, "coach_id", None):
            availability.coach = request.user

        availability.save()
        messages.success(request, "空き枠を作成しました。")
        return redirect("club:coach_availability_list")

    return render(
        request,
        _coach_availability_create_template_name(),
        {
            "form": form,
        },
    )


@login_required
def coach_availability_delete(request, pk):
    if CoachAvailability is None:
        raise Http404("Coach availability feature is not available.")

    availability = get_object_or_404(CoachAvailability, pk=pk)

    owner_coach_id = _get_object_coach_id(availability)
    can_delete = (
        request.user.is_superuser
        or request.user.is_staff
        or str(owner_coach_id) == str(request.user.pk)
    )

    if not can_delete:
        raise Http404()

    availability.delete()
    messages.success(request, "空き枠を削除しました。")
    return redirect("club:coach_availability_list")


@login_required
def line_link_page(request):
    link = None

    if LineAccountLink is not None:
        user_field_name = None
        for candidate in ["user", "member", "customer"]:
            if hasattr(LineAccountLink, candidate):
                user_field_name = candidate
                break

        if user_field_name:
            try:
                link = LineAccountLink.objects.filter(**{user_field_name: request.user}).first()
            except Exception:
                link = None

    form = None
    if LineAccountLinkForm is not None:
        form = LineAccountLinkForm(request.POST or None, instance=link)
        if request.method == "POST" and form.is_valid():
            obj = form.save(commit=False)

            for candidate in ["user", "member", "customer"]:
                if hasattr(obj, f"{candidate}_id") and not getattr(obj, f"{candidate}_id", None):
                    setattr(obj, candidate, request.user)
                    break

            obj.save()
            messages.success(request, "LINE連携情報を保存しました。")
            return redirect("club:line_link")

    return render(
        request,
        _line_link_template_name(),
        {
            "form": form,
            "line_link": link,
        },
    )
