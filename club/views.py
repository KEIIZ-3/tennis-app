import json
import secrets
from urllib.parse import urlencode

from django.apps import apps
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.core import signing
from django.core.exceptions import ValidationError
from django.forms import modelform_factory
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import forms as club_forms
from .notifications import (
    build_reservation_canceled_message,
    build_reservation_created_message,
    notify_user,
    send_line_reply,
    verify_line_signature,
)


def _get_model(name):
    try:
        return apps.get_model("club", name)
    except Exception:
        return None


def _get_form(name):
    return getattr(club_forms, name, None)


Reservation = _get_model("Reservation")
CoachAvailability = _get_model("CoachAvailability")
LineAccountLink = _get_model("LineAccountLink")

ReservationCreateForm = _get_form("ReservationCreateForm")
ReservationForm = _get_form("ReservationForm")
LineAccountLinkForm = _get_form("LineAccountLinkForm")


def _pick_first_attr(obj, names, default=None):
    for name in names:
        value = getattr(obj, name, None)
        if value not in (None, ""):
            return value
    return default


def _pick_datetime(obj, names):
    return _pick_first_attr(obj, names, None)


def _to_event_datetime_str(value):
    if not value:
        return None
    try:
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.isoformat()
    except Exception:
        return str(value)


def _is_staff_like(user):
    if not user or not user.is_authenticated:
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True

    role = getattr(user, "role", None)
    return role in ("coach", "admin", "staff", "manager")


def _is_coach_user(user):
    if not user or not user.is_authenticated:
        return False
    return getattr(user, "role", None) == "coach"


def _get_user_queryset_field(model):
    if model is None:
        return None
    candidate_fields = ["user", "customer", "member", "owner"]
    model_fields = {f.name for f in model._meta.fields}
    for name in candidate_fields:
        if name in model_fields:
            return name
    return None


def _get_datetime_field_names(model):
    if model is None:
        return (None, None)

    model_fields = {f.name for f in model._meta.fields}
    start_candidates = ["start_at", "start", "starts_at", "start_datetime"]
    end_candidates = ["end_at", "end", "ends_at", "end_datetime"]

    start_field = next((n for n in start_candidates if n in model_fields), None)
    end_field = next((n for n in end_candidates if n in model_fields), None)
    return start_field, end_field


def _build_reservation_form():
    form_class = ReservationCreateForm or ReservationForm
    if form_class is not None:
        return form_class

    if Reservation is None:
        return None

    model_fields = {f.name for f in Reservation._meta.fields}
    candidate_order = [
        "coach_availability",
        "availability",
        "coach",
        "court",
        "date",
        "start_at",
        "end_at",
        "note",
        "remarks",
        "comment",
    ]
    fields = [name for name in candidate_order if name in model_fields]

    if not fields:
        exclude = ["id", "created_at", "updated_at"]
        fields = [f.name for f in Reservation._meta.fields if f.name not in exclude]

    return modelform_factory(Reservation, fields=fields)


def _build_coach_availability_form():
    form_class = _get_form("CoachAvailabilityForm")
    if form_class is not None:
        return form_class

    if CoachAvailability is None:
        return None

    model_fields = {f.name for f in CoachAvailability._meta.fields}
    candidate_order = [
        "coach",
        "court",
        "start_at",
        "end_at",
        "capacity",
        "is_active",
        "note",
        "remarks",
        "comment",
    ]
    fields = [name for name in candidate_order if name in model_fields]

    if not fields:
        exclude = ["id", "created_at", "updated_at"]
        fields = [f.name for f in CoachAvailability._meta.fields if f.name not in exclude]

    return modelform_factory(CoachAvailability, fields=fields)


def _apply_logged_in_user_to_instance(instance, user):
    if not instance or not user or not user.is_authenticated:
        return

    for attr in ("user", "customer", "member", "owner"):
        if hasattr(instance, attr):
            current_value = getattr(instance, attr, None)
            if current_value in (None, ""):
                try:
                    setattr(instance, attr, user)
                except Exception:
                    pass
            break


def _apply_logged_in_coach_to_instance(instance, user):
    if not instance or not user or not user.is_authenticated:
        return

    if not _is_coach_user(user) and not _is_staff_like(user):
        return

    if hasattr(instance, "coach"):
        current_value = getattr(instance, "coach", None)
        if current_value in (None, ""):
            try:
                setattr(instance, "coach", user)
            except Exception:
                pass


def _user_can_access_reservation(user, reservation):
    if not user or not user.is_authenticated:
        return False
    if _is_staff_like(user):
        return True

    for attr in ("user", "customer", "member", "owner"):
        value = getattr(reservation, attr, None)
        if value == user:
            return True

    coach = getattr(reservation, "coach", None)
    if coach == user:
        return True

    availability = _pick_first_attr(reservation, ["coach_availability", "availability"], None)
    if availability is not None:
        availability_coach = getattr(availability, "coach", None)
        if availability_coach == user:
            return True

    return False


def _is_reservation_canceled(reservation):
    is_canceled = bool(getattr(reservation, "is_canceled", False))
    status = str(getattr(reservation, "status", "") or "").lower()
    return is_canceled or status == "canceled"


def _cancel_reservation_instance(instance):
    updated = False

    if hasattr(instance, "is_canceled"):
        setattr(instance, "is_canceled", True)
        updated = True

    if hasattr(instance, "canceled_at"):
        setattr(instance, "canceled_at", timezone.now())
        updated = True

    if hasattr(instance, "status"):
        try:
            setattr(instance, "status", "canceled")
            updated = True
        except Exception:
            pass

    if updated:
        instance.save()
    else:
        instance.delete()


def _find_line_link_for_user(user):
    if LineAccountLink is None or not user or not user.is_authenticated:
        return None
    try:
        return LineAccountLink.objects.filter(user=user).first()
    except Exception:
        return None


def _generate_line_link_token(user):
    if not user or not user.is_authenticated:
        return ""

    signer = signing.TimestampSigner(salt="club.line.link")
    raw = f"line-link:{user.pk}:{secrets.token_hex(8)}"
    return signer.sign(raw)


def _extract_line_link_token_from_text(text):
    if not text:
        return ""

    text = str(text).strip()

    prefixes = [
        "LINK ",
        "LINK:",
        "連携 ",
        "連携:",
        "link ",
        "link:",
    ]
    for prefix in prefixes:
        if text.startswith(prefix):
            return text[len(prefix):].strip()

    return text


def _resolve_user_from_link_token(token):
    signer = signing.TimestampSigner(salt="club.line.link")
    try:
        value = signer.unsign(token, max_age=60 * 60 * 24 * 30)
    except Exception:
        return None

    parts = value.split(":")
    if len(parts) < 2 or parts[0] != "line-link":
        return None

    user_pk = parts[1]
    User = get_user_model()
    try:
        return User.objects.filter(pk=user_pk).first()
    except Exception:
        return None


def _parse_query_datetime(value):
    if not value:
        return None
    dt = parse_datetime(value)
    if dt is None:
        return None
    if timezone.is_aware(dt):
        return timezone.localtime(dt)
    return timezone.make_aware(dt)


def home(request):
    User = get_user_model()
    coaches = User.objects.filter(role="coach").order_by("username")
    selected_coach = request.GET.get("coach", "")
    return render(
        request,
        "home.html",
        {
            "coaches": coaches,
            "selected_coach": selected_coach,
        },
    )


@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        return redirect("club:home")

    form = AuthenticationForm(request, data=request.POST or None)

    if request.method == "POST" and form.is_valid():
        login(request, form.get_user())
        return redirect("club:home")

    return render(request, "login.html", {"form": form})


@login_required
@require_POST
def logout_view(request):
    logout(request)
    return redirect("club:login")


@require_GET
def healthz(request):
    return JsonResponse({"ok": True})


@login_required
@require_GET
def calendar_events(request):
    events = []
    coach_filter = request.GET.get("coach") or request.GET.get("coach_id")

    if CoachAvailability is not None:
        qs = CoachAvailability.objects.all()

        if coach_filter and hasattr(CoachAvailability, "coach_id"):
            try:
                qs = qs.filter(coach_id=coach_filter)
            except Exception:
                pass

        for obj in qs:
            start_at = _pick_datetime(obj, ["start_at", "start", "starts_at", "start_datetime"])
            end_at = _pick_datetime(obj, ["end_at", "end", "ends_at", "end_datetime"])
            coach = getattr(obj, "coach", None)
            court = getattr(obj, "court", None)

            title_parts = ["空き枠"]
            if coach:
                title_parts.append(str(coach))
            if court:
                title_parts.append(str(court))

            query = urlencode(
                {
                    "coach": getattr(coach, "pk", "") or "",
                    "court": getattr(court, "pk", "") or "",
                    "start": _to_event_datetime_str(start_at) or "",
                    "end": _to_event_datetime_str(end_at) or "",
                }
            )

            events.append(
                {
                    "id": f"availability-{obj.pk}",
                    "title": " / ".join(title_parts),
                    "start": _to_event_datetime_str(start_at),
                    "end": _to_event_datetime_str(end_at),
                    "display": "auto",
                    "backgroundColor": "#22c55e",
                    "borderColor": "#22c55e",
                    "extendedProps": {
                        "kind": "availability",
                        "type": "availability",
                        "pk": obj.pk,
                        "coach": str(coach) if coach else "",
                        "coach_name": str(coach) if coach else "",
                        "court": str(court) if court else "",
                        "reserve_url": f"{reverse('club:reservation_create')}?{query}",
                    },
                }
            )

    if Reservation is not None:
        qs = Reservation.objects.all()

        if coach_filter and hasattr(Reservation, "coach_id"):
            try:
                qs = qs.filter(coach_id=coach_filter)
            except Exception:
                pass

        for obj in qs:
            start_at = _pick_datetime(obj, ["start_at", "start", "starts_at", "start_datetime"])
            end_at = _pick_datetime(obj, ["end_at", "end", "ends_at", "end_datetime"])
            availability = _pick_first_attr(obj, ["coach_availability", "availability"], None)

            if not start_at and availability is not None:
                start_at = _pick_datetime(availability, ["start_at", "start", "starts_at", "start_datetime"])
            if not end_at and availability is not None:
                end_at = _pick_datetime(availability, ["end_at", "end", "ends_at", "end_datetime"])

            coach = getattr(obj, "coach", None)
            if not coach and availability is not None:
                coach = getattr(availability, "coach", None)

            if coach_filter and coach is not None:
                try:
                    if str(getattr(coach, "pk", "")) != str(coach_filter):
                        continue
                except Exception:
                    pass

            court = getattr(obj, "court", None)
            if not court and availability is not None:
                court = getattr(availability, "court", None)

            user_obj = _pick_first_attr(obj, ["user", "customer", "member", "owner"], None)
            is_canceled = _is_reservation_canceled(obj)
            is_mine = bool(user_obj == request.user)
            cancel_url = reverse("club:reservation_cancel", kwargs={"pk": obj.pk}) if is_mine and not is_canceled else ""

            if is_canceled:
                event_title = "キャンセル済み"
                background_color = "#9ca3af"
                border_color = "#9ca3af"
            else:
                event_title = "あなたの予約" if is_mine else "予約済み"
                background_color = "#3b82f6" if is_mine else "#ef4444"
                border_color = background_color

            events.append(
                {
                    "id": f"reservation-{obj.pk}",
                    "title": event_title,
                    "start": _to_event_datetime_str(start_at),
                    "end": _to_event_datetime_str(end_at),
                    "display": "auto",
                    "backgroundColor": background_color,
                    "borderColor": border_color,
                    "extendedProps": {
                        "kind": "reservation",
                        "type": "reservation",
                        "pk": obj.pk,
                        "user": str(user_obj) if user_obj else "",
                        "coach": str(coach) if coach else "",
                        "coach_name": str(coach) if coach else "",
                        "court": str(court) if court else "",
                        "is_canceled": is_canceled,
                        "is_mine": is_mine,
                        "cancel_url": cancel_url,
                    },
                }
            )

    return JsonResponse(events, safe=False)


@login_required
@require_http_methods(["GET", "POST"])
def reservation_create(request):
    if Reservation is None:
        raise Http404("Reservation model not found.")

    FormClass = _build_reservation_form()
    if FormClass is None:
        raise Http404("Reservation form not found.")

    initial = {}

    coach_id = request.GET.get("coach")
    court_id = request.GET.get("court")
    start_value = _parse_query_datetime(request.GET.get("start"))
    end_value = _parse_query_datetime(request.GET.get("end"))

    if coach_id:
        initial["coach"] = coach_id
    if court_id:
        initial["court"] = court_id
    if start_value:
        initial["start_at"] = start_value
    if end_value:
        initial["end_at"] = end_value

    form = FormClass(
        request.POST or None,
        request_user=request.user,
        initial=initial,
    )

    if request.method == "POST":
        if form.is_valid():
            try:
                reservation = form.save(commit=False)
                _apply_logged_in_user_to_instance(reservation, request.user)

                if hasattr(reservation, "status") and not getattr(reservation, "status", None):
                    try:
                        reservation.status = "booked"
                    except Exception:
                        pass

                if hasattr(reservation, "is_canceled"):
                    try:
                        reservation.is_canceled = False
                    except Exception:
                        pass

                if hasattr(reservation, "full_clean"):
                    reservation.full_clean()

                reservation.save()

                if hasattr(form, "save_m2m"):
                    try:
                        form.save_m2m()
                    except Exception:
                        pass

                try:
                    message = build_reservation_created_message(reservation)
                    notify_user(request.user, message)
                except Exception:
                    pass

                messages.success(request, "予約を作成しました。")
                return redirect("club:reservation_list")

            except ValidationError as e:
                if hasattr(e, "message_dict"):
                    for field_name, error_list in e.message_dict.items():
                        for error in error_list:
                            if field_name in form.fields:
                                form.add_error(field_name, error)
                            else:
                                form.add_error(None, error)
                else:
                    form.add_error(None, str(e))
            except Exception as e:
                form.add_error(None, f"予約保存時にエラーが発生しました: {e}")

        messages.error(request, "予約を作成できませんでした。入力内容をご確認ください。")

    return render(
        request,
        "reservations/create.html",
        {
            "form": form,
        },
    )


@login_required
@require_GET
def reservation_list(request):
    if Reservation is None:
        raise Http404("Reservation model not found.")

    qs = Reservation.objects.all()

    if not _is_staff_like(request.user):
        user_field = _get_user_queryset_field(Reservation)
        if user_field:
            qs = qs.filter(**{user_field: request.user})
        else:
            filtered_ids = []
            for obj in qs:
                if _user_can_access_reservation(request.user, obj):
                    filtered_ids.append(obj.pk)
            qs = qs.filter(pk__in=filtered_ids)

    start_field, _ = _get_datetime_field_names(Reservation)
    if start_field:
        try:
            qs = qs.order_by(start_field)
        except Exception:
            pass
    else:
        try:
            qs = qs.order_by("-pk")
        except Exception:
            pass

    return render(
        request,
        "reservations/list.html",
        {
            "reservations": qs,
        },
    )


@login_required
@require_POST
def reservation_cancel(request, pk):
    if Reservation is None:
        raise Http404("Reservation model not found.")

    reservation = get_object_or_404(Reservation, pk=pk)

    if not _user_can_access_reservation(request.user, reservation):
        return HttpResponse("Forbidden", status=403)

    if _is_reservation_canceled(reservation):
        messages.info(request, "この予約はすでにキャンセル済みです。")
        return redirect("club:reservation_list")

    try:
        _cancel_reservation_instance(reservation)
    except Exception as e:
        messages.error(request, f"予約のキャンセルに失敗しました: {e}")
        return redirect("club:reservation_list")

    try:
        message = build_reservation_canceled_message(reservation)
        notify_user(request.user, message)
    except Exception:
        pass

    messages.success(request, "予約をキャンセルしました。")
    return redirect("club:reservation_list")


@login_required
@require_GET
def coach_availability_list(request):
    if CoachAvailability is None:
        raise Http404("CoachAvailability model not found.")

    qs = CoachAvailability.objects.all()

    if _is_coach_user(request.user) and hasattr(CoachAvailability, "coach"):
        try:
            qs = qs.filter(coach=request.user)
        except Exception:
            pass

    start_field, _ = _get_datetime_field_names(CoachAvailability)
    if start_field:
        try:
            qs = qs.order_by(start_field)
        except Exception:
            pass

    return render(
        request,
        "coach/availability_list.html",
        {
            "availabilities": qs,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def coach_availability_create(request):
    if CoachAvailability is None:
        raise Http404("CoachAvailability model not found.")

    FormClass = _build_coach_availability_form()
    if FormClass is None:
        raise Http404("CoachAvailability form not found.")

    form = FormClass(
        request.POST or None,
        request_user=request.user,
    )

    if request.method == "POST":
        if form.is_valid():
            availability = form.save(commit=False)
            _apply_logged_in_coach_to_instance(availability, request.user)
            availability.save()

            if hasattr(form, "save_m2m"):
                try:
                    form.save_m2m()
                except Exception:
                    pass

            messages.success(request, "コーチ空き時間を登録しました。")
            return redirect("club:coach_availability_list")

        messages.error(request, "コーチ空き時間を登録できませんでした。入力内容をご確認ください。")

    return render(
        request,
        "coach/availability_create.html",
        {
            "form": form,
        },
    )


@login_required
@require_POST
def coach_availability_delete(request, pk):
    if CoachAvailability is None:
        raise Http404("CoachAvailability model not found.")

    availability = get_object_or_404(CoachAvailability, pk=pk)

    if not _is_staff_like(request.user):
        coach = getattr(availability, "coach", None)
        if coach != request.user:
            return HttpResponse("Forbidden", status=403)

    availability.delete()
    messages.success(request, "コーチ空き時間を削除しました。")
    return redirect("club:coach_availability_list")


@login_required
@require_http_methods(["GET"])
def line_connect(request):
    link = _find_line_link_for_user(request.user)
    link_token = _generate_line_link_token(request.user)

    context = {
        "line_link": link,
        "line_link_token": link_token,
        "manual_form": LineAccountLinkForm() if LineAccountLinkForm else None,
    }
    return render(request, "line_connect.html", context)


@login_required
@require_http_methods(["POST"])
def line_link(request):
    if LineAccountLink is None:
        messages.error(request, "LineAccountLink モデルが見つかりません。")
        return redirect("club:line_connect")

    action = (request.POST.get("action") or "").strip()

    if action == "unlink":
        link = _find_line_link_for_user(request.user)
        if link:
            try:
                link.is_active = False
                link.save()
                messages.success(request, "LINE連携を解除しました。")
            except Exception as e:
                messages.error(request, f"LINE連携の解除に失敗しました: {e}")
        else:
            messages.info(request, "解除対象の連携はありません。")
        return redirect("club:line_connect")

    if LineAccountLinkForm is not None:
        form = LineAccountLinkForm(request.POST)
        if form.is_valid():
            line_user_id = form.cleaned_data.get("line_user_id")
            is_active = form.cleaned_data.get("is_active", True)

            try:
                LineAccountLink.objects.update_or_create(
                    user=request.user,
                    defaults={
                        "line_user_id": line_user_id,
                        "is_active": is_active,
                    },
                )
                messages.success(request, "LINE連携情報を保存しました。")
            except Exception as e:
                messages.error(request, f"LINE連携情報の保存に失敗しました: {e}")
        else:
            messages.error(request, "入力内容をご確認ください。")
        return redirect("club:line_connect")

    line_user_id = (request.POST.get("line_user_id") or "").strip()
    is_active = request.POST.get("is_active") in ("1", "true", "True", "on")

    if not line_user_id:
        messages.error(request, "line_user_id を入力してください。")
        return redirect("club:line_connect")

    try:
        LineAccountLink.objects.update_or_create(
            user=request.user,
            defaults={
                "line_user_id": line_user_id,
                "is_active": is_active,
            },
        )
        messages.success(request, "LINE連携情報を保存しました。")
    except Exception as e:
        messages.error(request, f"LINE連携情報の保存に失敗しました: {e}")

    return redirect("club:line_connect")


@csrf_exempt
@require_http_methods(["POST"])
def line_webhook(request):
    signature = request.META.get("HTTP_X_LINE_SIGNATURE", "")
    body = request.body or b""

    if not verify_line_signature(body, signature):
        return HttpResponse("Invalid signature", status=400)

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return HttpResponse("Invalid JSON", status=400)

    events = payload.get("events", [])
    for event in events:
        event_type = event.get("type")
        source = event.get("source", {}) or {}
        line_user_id = source.get("userId", "")
        reply_token = event.get("replyToken", "")

        if event_type == "message":
            message = event.get("message", {}) or {}
            if message.get("type") != "text":
                continue

            text = (message.get("text") or "").strip()
            token = _extract_line_link_token_from_text(text)
            user = _resolve_user_from_link_token(token)

            if user is None:
                if reply_token:
                    send_line_reply(
                        reply_token,
                        "連携コードを確認できませんでした。画面に表示されたコードをそのまま送信してください。",
                    )
                continue

            if LineAccountLink is None:
                if reply_token:
                    send_line_reply(
                        reply_token,
                        "サーバー側でLINE連携モデルが見つかりませんでした。",
                    )
                continue

            try:
                LineAccountLink.objects.update_or_create(
                    user=user,
                    defaults={
                        "line_user_id": line_user_id,
                        "is_active": True,
                    },
                )
                if reply_token:
                    send_line_reply(
                        reply_token,
                        "LINE連携が完了しました。今後、予約通知などを受け取れるようになります。",
                    )
            except Exception:
                if reply_token:
                    send_line_reply(
                        reply_token,
                        "LINE連携の保存中にエラーが発生しました。",
                    )

        elif event_type == "follow":
            if reply_token:
                send_line_reply(
                    reply_token,
                    "友だち追加ありがとうございます。アプリのLINE連携画面に表示される連携コードを、このトークに送信してください。",
                )

    return HttpResponse("OK")
