import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .forms import (
    CoachAvailabilityForm,
    LineAccountLinkForm,
    LineProfileCompletionForm,
    MemberRegistrationForm,
    ReservationCreateForm,
)
from .models import (
    CoachAvailability,
    FixedLesson,
    LineAccountLink,
    Reservation,
    TicketLedger,
)
from .notifications import (
    build_reservation_canceled_message,
    build_reservation_created_message,
    notify_user,
    send_line_reply,
    verify_line_signature,
)


def _display_name(user):
    if not user:
        return "ユーザー"
    try:
        return user.display_name()
    except Exception:
        return getattr(user, "username", "ユーザー")


def _is_staff_like(user):
    if not user or not user.is_authenticated:
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    return getattr(user, "role", None) in ("coach", "admin", "staff", "manager")


def _is_coach_user(user):
    if not user or not user.is_authenticated:
        return False
    return getattr(user, "role", None) == "coach"


def _to_event_datetime_str(value):
    if not value:
        return None
    try:
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.isoformat()
    except Exception:
        return str(value)


def _login_user_with_default_backend(request, user):
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")


def _normalize_next_url(value):
    if not value:
        return reverse("club:home")
    value = str(value).strip()
    if not value.startswith("/"):
        return reverse("club:home")
    if value.startswith("//"):
        return reverse("club:home")
    return value


def _parse_query_datetime(value):
    if not value:
        return None
    dt = parse_datetime(value)
    if dt is None:
        return None
    if timezone.is_aware(dt):
        return timezone.localtime(dt)
    return timezone.make_aware(dt)


def _line_login_enabled():
    return bool(
        getattr(settings, "LINE_LOGIN_CHANNEL_ID", "").strip()
        and getattr(settings, "LINE_LOGIN_CHANNEL_SECRET", "").strip()
    )


def _line_login_redirect_uri(request):
    configured = getattr(settings, "LINE_LOGIN_REDIRECT_URI", "").strip()
    if configured:
        return configured
    return request.build_absolute_uri(reverse("club:line_login_callback"))


def _line_login_scope():
    scope = getattr(settings, "LINE_LOGIN_SCOPE", "openid profile").strip()
    return scope or "openid profile"


def _liff_enabled():
    return bool(
        getattr(settings, "LINE_LIFF_ID", "").strip()
        and getattr(settings, "LINE_LOGIN_CHANNEL_ID", "").strip()
    )


def _post_form_urlencoded(url, params):
    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _exchange_line_login_code_for_token(request, code):
    return _post_form_urlencoded(
        "https://api.line.me/oauth2/v2.1/token",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _line_login_redirect_uri(request),
            "client_id": getattr(settings, "LINE_LOGIN_CHANNEL_ID", "").strip(),
            "client_secret": getattr(settings, "LINE_LOGIN_CHANNEL_SECRET", "").strip(),
        },
    )


def _verify_line_id_token(id_token, nonce=None):
    payload = {
        "id_token": id_token,
        "client_id": getattr(settings, "LINE_LOGIN_CHANNEL_ID", "").strip(),
    }
    if nonce:
        payload["nonce"] = nonce

    return _post_form_urlencoded(
        "https://api.line.me/oauth2/v2.1/verify",
        payload,
    )


def _generate_unique_line_username(line_user_id):
    User = get_user_model()

    base = f"line_{str(line_user_id)[-12:]}"
    base = base[:150] or f"line_{secrets.token_hex(6)}"

    username = base
    counter = 1
    while User.objects.filter(username=username).exists():
        suffix = f"_{counter}"
        username = f"{base[:150 - len(suffix)]}{suffix}"
        counter += 1

    return username


def _needs_profile_completion(user):
    if not user:
        return False

    if not getattr(user, "is_profile_completed", False):
        return True

    if not (getattr(user, "full_name", "") or "").strip():
        return True

    if not (getattr(user, "email", "") or "").strip():
        return True

    if not (getattr(user, "phone_number", "") or "").strip():
        return True

    return False


def _require_profile_completed_for_booking(request):
    if _needs_profile_completion(request.user):
        messages.info(request, "予約の前に会員情報の入力を完了してください。")
        return redirect("club:profile_complete")
    return None


def _find_line_link_for_user(user):
    if not user or not user.is_authenticated:
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


def _upsert_user_by_line_identity(request, line_user_id, email="", picture_url=""):
    User = get_user_model()
    now = timezone.now()

    if request.user.is_authenticated:
        conflict = LineAccountLink.objects.filter(line_user_id=line_user_id).exclude(user=request.user).first()
        if conflict:
            raise RuntimeError("このLINEアカウントは別の会員に連携済みです。")

        LineAccountLink.objects.update_or_create(
            user=request.user,
            defaults={
                "line_user_id": line_user_id,
                "is_active": True,
                "last_event_at": now,
            },
        )
        return request.user, "linked"

    existing_link = LineAccountLink.objects.select_related("user").filter(line_user_id=line_user_id).first()
    if existing_link and existing_link.user:
        existing_link.is_active = True
        existing_link.last_event_at = now
        existing_link.save(update_fields=["is_active", "last_event_at"])
        return existing_link.user, "logged_in"

    username = _generate_unique_line_username(line_user_id)
    user = User(username=username)

    if hasattr(user, "role") and not getattr(user, "role", None):
        user.role = "member"

    if hasattr(user, "email") and email:
        user.email = email[:254]

    if hasattr(user, "first_name") and not getattr(user, "first_name", None):
        user.first_name = ""

    if hasattr(user, "last_name") and not getattr(user, "last_name", None):
        user.last_name = ""

    if hasattr(user, "full_name"):
        user.full_name = ""

    if hasattr(user, "phone_number"):
        user.phone_number = ""

    if hasattr(user, "is_profile_completed"):
        user.is_profile_completed = False

    user.set_unusable_password()
    user.save()

    LineAccountLink.objects.update_or_create(
        user=user,
        defaults={
            "line_user_id": line_user_id,
            "is_active": True,
            "last_event_at": now,
        },
    )
    return user, "created"


def _sync_fixed_lessons():
    try:
        queryset = FixedLesson.objects.filter(is_active=True).prefetch_related("members")
        for fixed_lesson in queryset:
            try:
                fixed_lesson.sync_future_reservations()
            except Exception:
                continue
    except Exception:
        pass


def _user_can_access_reservation(user, reservation):
    if not user or not user.is_authenticated:
        return False
    if _is_staff_like(user):
        return True
    if reservation.user_id == user.pk:
        return True
    if reservation.coach_id == user.pk:
        return True
    return False


def _is_reservation_canceled(reservation):
    return reservation.status in (Reservation.STATUS_CANCELED, Reservation.STATUS_RAIN_CANCELED)


def _can_user_cancel_reservation(user, reservation):
    if not _user_can_access_reservation(user, reservation):
        return False, "この予約を操作する権限がありません。"

    if _is_reservation_canceled(reservation):
        return False, "この予約はすでにキャンセル済みです。"

    if _is_staff_like(user) or reservation.coach_id == getattr(user, "pk", None):
        return True, ""

    active_count = reservation.active_count_in_same_slot()
    if active_count <= 1:
        return False, "最後の1名となるため、この予約はキャンセルできません。"

    return True, ""


def _lesson_type_label(lesson_type):
    if lesson_type == Reservation.LESSON_PRIVATE:
        return "プライベートレッスン"
    return "一般レッスン"


def home(request):
    if request.user.is_authenticated:
        _sync_fixed_lessons()

    User = get_user_model()
    coaches = User.objects.filter(role="coach").order_by("username")
    selected_coach = request.GET.get("coach", "")

    return render(
        request,
        "home.html",
        {
            "coaches": coaches,
            "selected_coach": selected_coach,
            "liff_enabled": _liff_enabled(),
        },
    )


@never_cache
@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        if _needs_profile_completion(request.user):
            return redirect("club:profile_complete")
        return redirect("club:home")

    form = AuthenticationForm(request, data=request.POST or None)

    if request.method == "POST":
        if form.is_valid():
            login(request, form.get_user())
            if _needs_profile_completion(request.user):
                return redirect("club:profile_complete")
            return redirect("club:home")
        messages.error(request, "ユーザー名またはパスワードが正しくありません。")

    return render(
        request,
        "login.html",
        {
            "form": form,
            "liff_enabled": _liff_enabled(),
        },
    )


@never_cache
@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def register_view(request):
    if request.user.is_authenticated:
        if _needs_profile_completion(request.user):
            return redirect("club:profile_complete")
        return redirect("club:home")

    form = MemberRegistrationForm(request.POST or None)

    if request.method == "POST":
        if form.is_valid():
            user = form.save()
            _login_user_with_default_backend(request, user)
            messages.success(request, "新規会員登録が完了しました。")
            return redirect("club:home")

        messages.error(request, "新規会員登録できませんでした。入力内容をご確認ください。")

    return render(
        request,
        "register.html",
        {
            "form": form,
            "liff_enabled": _liff_enabled(),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def profile_complete_view(request):
    if request.method == "GET" and not _needs_profile_completion(request.user):
        return redirect("club:home")

    form = LineProfileCompletionForm(request.POST or None, instance=request.user)

    if request.method == "POST":
        if form.is_valid():
            form.save()
            messages.success(request, "会員情報の登録が完了しました。")
            return redirect("club:home")
        messages.error(request, "会員情報を保存できませんでした。入力内容をご確認ください。")

    return render(
        request,
        "profile_complete.html",
        {
            "form": form,
        },
    )


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
    _sync_fixed_lessons()

    events = []
    coach_filter = request.GET.get("coach") or request.GET.get("coach_id")

    availability_qs = CoachAvailability.objects.all()
    if coach_filter:
        availability_qs = availability_qs.filter(coach_id=coach_filter)

    for obj in availability_qs:
        coach = obj.coach
        court = obj.court

        title_parts = [
            "空き枠",
            _lesson_type_label(obj.lesson_type),
            str(coach),
            str(court),
        ]

        query = urlencode(
            {
                "coach": getattr(coach, "pk", "") or "",
                "court": getattr(court, "pk", "") or "",
                "lesson_type": obj.lesson_type,
                "start": _to_event_datetime_str(obj.start_at) or "",
                "end": _to_event_datetime_str(obj.end_at) or "",
            }
        )

        events.append(
            {
                "id": f"availability-{obj.pk}",
                "title": " / ".join(title_parts),
                "start": _to_event_datetime_str(obj.start_at),
                "end": _to_event_datetime_str(obj.end_at),
                "display": "auto",
                "backgroundColor": "#22c55e",
                "borderColor": "#22c55e",
                "extendedProps": {
                    "kind": "availability",
                    "type": "availability",
                    "pk": obj.pk,
                    "coach_name": str(coach),
                    "court": str(court),
                    "lesson_type_display": _lesson_type_label(obj.lesson_type),
                    "capacity": obj.capacity,
                    "reserve_url": f"{reverse('club:reservation_create')}?{query}",
                },
            }
        )

    reservation_qs = Reservation.objects.select_related("user", "coach", "court").all()
    if coach_filter:
        reservation_qs = reservation_qs.filter(coach_id=coach_filter)

    for obj in reservation_qs:
        is_canceled = _is_reservation_canceled(obj)
        is_mine = bool(obj.user_id == request.user.pk)
        can_cancel, cancel_reason = _can_user_cancel_reservation(request.user, obj)
        cancel_url = reverse("club:reservation_cancel", kwargs={"pk": obj.pk}) if can_cancel else ""

        if obj.status == Reservation.STATUS_RAIN_CANCELED:
            event_title = "雨天中止"
            background_color = "#6b7280"
        elif is_canceled:
            event_title = "キャンセル済み"
            background_color = "#9ca3af"
        else:
            event_title = "あなたの予約" if is_mine else f"予約済み ({_display_name(obj.user)})"
            background_color = "#3b82f6" if is_mine else "#ef4444"

        events.append(
            {
                "id": f"reservation-{obj.pk}",
                "title": event_title,
                "start": _to_event_datetime_str(obj.start_at),
                "end": _to_event_datetime_str(obj.end_at),
                "display": "auto",
                "backgroundColor": background_color,
                "borderColor": background_color,
                "extendedProps": {
                    "kind": "reservation",
                    "type": "reservation",
                    "pk": obj.pk,
                    "user_name": _display_name(obj.user),
                    "coach_name": str(obj.coach),
                    "court": str(obj.court),
                    "lesson_type_display": _lesson_type_label(obj.lesson_type),
                    "tickets_used": obj.tickets_used,
                    "is_canceled": is_canceled,
                    "is_mine": is_mine,
                    "can_cancel": can_cancel,
                    "cancel_url": cancel_url,
                    "cancel_reason": cancel_reason,
                    "status_display": obj.get_status_display(),
                },
            }
        )

    return JsonResponse(events, safe=False)


@login_required
@require_http_methods(["GET", "POST"])
def reservation_create(request):
    profile_redirect = _require_profile_completed_for_booking(request)
    if profile_redirect:
        return profile_redirect

    _sync_fixed_lessons()

    initial = {}
    coach_id = request.GET.get("coach")
    court_id = request.GET.get("court")
    lesson_type = request.GET.get("lesson_type") or Reservation.LESSON_GROUP
    start_value = _parse_query_datetime(request.GET.get("start"))
    end_value = _parse_query_datetime(request.GET.get("end"))

    if not end_value and start_value:
        end_value = start_value + timedelta(hours=Reservation.duration_hours_for_lesson_type(lesson_type))

    if coach_id:
        initial["coach"] = coach_id
    if court_id:
        initial["court"] = court_id
    if lesson_type:
        initial["lesson_type"] = lesson_type
    if start_value:
        initial["start_at"] = start_value
    if end_value:
        initial["end_at"] = end_value

    form = ReservationCreateForm(
        request.POST or None,
        request_user=request.user,
        initial=initial,
    )

    if request.method == "POST":
        if form.is_valid():
            try:
                with transaction.atomic():
                    reservation = form.save(commit=False)
                    reservation.user = request.user
                    reservation.status = Reservation.STATUS_ACTIVE
                    reservation.full_clean()
                    reservation.save()
                    reservation.consume_tickets(
                        reason=TicketLedger.REASON_RESERVATION_USE,
                        created_by=request.user,
                        note=f"予約作成時に消費: {reservation.start_at:%Y-%m-%d %H:%M}",
                    )

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
    _sync_fixed_lessons()

    qs = Reservation.objects.select_related("user", "coach", "court").all()

    if _is_staff_like(request.user):
        pass
    elif _is_coach_user(request.user):
        qs = qs.filter(coach=request.user)
    else:
        qs = qs.filter(user=request.user)

    qs = qs.order_by("start_at")

    reservation_rows = []
    for reservation in qs:
        can_cancel, cancel_reason = _can_user_cancel_reservation(request.user, reservation)
        reservation_rows.append(
            {
                "reservation": reservation,
                "can_cancel": can_cancel,
                "cancel_reason": cancel_reason,
            }
        )

    return render(
        request,
        "reservations/list.html",
        {
            "reservation_rows": reservation_rows,
        },
    )


@login_required
@require_POST
def reservation_cancel(request, pk):
    reservation = get_object_or_404(Reservation, pk=pk)

    if not _user_can_access_reservation(request.user, reservation):
        return HttpResponse("Forbidden", status=403)

    can_cancel, cancel_reason = _can_user_cancel_reservation(request.user, reservation)
    if not can_cancel:
        messages.error(request, cancel_reason)
        return redirect("club:reservation_list")

    try:
        with transaction.atomic():
            reservation.cancel(
                created_by=request.user,
                reason="会員キャンセル" if reservation.user_id == request.user.pk else "コーチ/管理者キャンセル",
            )
    except Exception as e:
        messages.error(request, f"予約のキャンセルに失敗しました: {e}")
        return redirect("club:reservation_list")

    try:
        message = build_reservation_canceled_message(reservation)
        notify_user(reservation.user, message)
    except Exception:
        pass

    messages.success(request, "予約をキャンセルしました。")
    return redirect("club:reservation_list")


@login_required
@require_GET
def coach_availability_list(request):
    _sync_fixed_lessons()

    qs = CoachAvailability.objects.select_related("coach", "court").all()

    if _is_coach_user(request.user):
        qs = qs.filter(coach=request.user)

    qs = qs.order_by("start_at")

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
    form = CoachAvailabilityForm(
        request.POST or None,
        request_user=request.user,
    )

    if request.method == "POST":
        if form.is_valid():
            availability = form.save(commit=False)
            if _is_coach_user(request.user) and not _is_staff_like(request.user):
                availability.coach = request.user
            availability.save()

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
    availability = get_object_or_404(CoachAvailability, pk=pk)

    if not _is_staff_like(request.user):
        if availability.coach != request.user:
            return HttpResponse("Forbidden", status=403)

    availability.delete()
    messages.success(request, "コーチ空き時間を削除しました。")
    return redirect("club:coach_availability_list")


@require_GET
def line_login_start(request):
    if not _line_login_enabled():
        messages.error(request, "LINE Login の設定が未完了です。")
        return redirect("club:login")

    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    next_url = _normalize_next_url(request.GET.get("next"))

    request.session["line_login_state"] = state
    request.session["line_login_nonce"] = nonce
    request.session["line_login_next"] = next_url

    params = {
        "response_type": "code",
        "client_id": getattr(settings, "LINE_LOGIN_CHANNEL_ID", "").strip(),
        "redirect_uri": _line_login_redirect_uri(request),
        "state": state,
        "scope": _line_login_scope(),
        "nonce": nonce,
    }

    authorize_url = "https://access.line.me/oauth2/v2.1/authorize?" + urllib.parse.urlencode(params)
    return redirect(authorize_url)


@require_GET
def line_login_callback(request):
    if not _line_login_enabled():
        messages.error(request, "LINE Login の設定が未完了です。")
        return redirect("club:login")

    error = (request.GET.get("error") or "").strip()
    if error:
        description = (request.GET.get("error_description") or "").strip()
        messages.error(request, f"LINEログインに失敗しました。{description or error}")
        return redirect("club:login")

    expected_state = request.session.pop("line_login_state", "")
    expected_nonce = request.session.pop("line_login_nonce", "")
    next_url = _normalize_next_url(request.session.pop("line_login_next", reverse("club:home")))

    actual_state = (request.GET.get("state") or "").strip()
    code = (request.GET.get("code") or "").strip()

    if not expected_state or expected_state != actual_state:
        messages.error(request, "LINEログインの state 検証に失敗しました。")
        return redirect("club:login")

    if not code:
        messages.error(request, "LINEログインの認証コードを受け取れませんでした。")
        return redirect("club:login")

    try:
        token_response = _exchange_line_login_code_for_token(request, code)
        id_token = (token_response.get("id_token") or "").strip()
        if not id_token:
            messages.error(request, "LINEログインの IDトークンを取得できませんでした。")
            return redirect("club:login")

        verified = _verify_line_id_token(id_token, expected_nonce)
    except urllib.error.HTTPError as e:
        try:
            error_body = e.read().decode("utf-8")
        except Exception:
            error_body = str(e)
        messages.error(request, f"LINEログインの通信に失敗しました。{error_body or str(e)}")
        return redirect("club:login")
    except Exception as e:
        messages.error(request, f"LINEログイン処理でエラーが発生しました: {e}")
        return redirect("club:login")

    line_user_id = str(verified.get("sub") or "").strip()
    email = str(verified.get("email") or "").strip()

    if not line_user_id:
        messages.error(request, "LINEのユーザーIDを取得できませんでした。")
        return redirect("club:login")

    try:
        user, result = _upsert_user_by_line_identity(
            request=request,
            line_user_id=line_user_id,
            email=email,
        )
        if result in ("created", "logged_in"):
            _login_user_with_default_backend(request, user)

        if result == "linked":
            messages.success(request, "LINEアカウントを自動連携しました。")
            return redirect("club:line_connect")

        if _needs_profile_completion(user):
            messages.info(request, "初回登録のため、会員情報を入力してください。")
            return redirect("club:profile_complete")

        if result == "created":
            messages.success(request, "LINEで新規登録・ログインしました。")
        else:
            messages.success(request, "LINEでログインしました。")
        return redirect(next_url)

    except Exception as e:
        messages.error(request, f"LINEアカウント連携でエラーが発生しました: {e}")
        return redirect("club:login")


@require_GET
def liff_entry(request):
    if not _liff_enabled():
        return HttpResponse("LIFF is not configured.", status=500)

    context = {
        "liff_id": getattr(settings, "LINE_LIFF_ID", "").strip(),
        "bootstrap_url": reverse("club:liff_bootstrap"),
        "home_url": reverse("club:home"),
    }
    return render(request, "liff_entry.html", context)


@csrf_exempt
@require_POST
def liff_bootstrap(request):
    if not _liff_enabled():
        return JsonResponse(
            {"ok": False, "message": "LIFF の設定が未完了です。"},
            status=500,
        )

    try:
        payload = json.loads((request.body or b"{}").decode("utf-8"))
    except Exception:
        return JsonResponse({"ok": False, "message": "不正なJSONです。"}, status=400)

    id_token = str(payload.get("idToken") or "").strip()
    picture_url = str(payload.get("pictureUrl") or "").strip()

    if not id_token:
        return JsonResponse(
            {"ok": False, "message": "idToken が取得できませんでした。"},
            status=400,
        )

    try:
        verified = _verify_line_id_token(id_token, None)
    except urllib.error.HTTPError as e:
        try:
            error_body = e.read().decode("utf-8")
        except Exception:
            error_body = str(e)
        return JsonResponse(
            {"ok": False, "message": f"LINEのIDトークン検証に失敗しました: {error_body}"},
            status=400,
        )
    except Exception as e:
        return JsonResponse(
            {"ok": False, "message": f"LINEのIDトークン検証でエラーが発生しました: {e}"},
            status=400,
        )

    line_user_id = str(verified.get("sub") or "").strip()
    verified_email = str(verified.get("email") or "").strip()

    if not line_user_id:
        return JsonResponse(
            {"ok": False, "message": "LINE userId を取得できませんでした。"},
            status=400,
        )

    try:
        user, result = _upsert_user_by_line_identity(
            request=request,
            line_user_id=line_user_id,
            email=verified_email,
            picture_url=picture_url,
        )

        if result in ("created", "logged_in"):
            _login_user_with_default_backend(request, user)

        if result == "linked":
            return JsonResponse(
                {
                    "ok": True,
                    "message": "LINEアカウントを連携しました。",
                    "redirectUrl": reverse("club:line_connect"),
                }
            )

        if _needs_profile_completion(user):
            return JsonResponse(
                {
                    "ok": True,
                    "message": "初回登録のため、会員情報を入力してください。",
                    "redirectUrl": reverse("club:profile_complete"),
                }
            )

        if result == "created":
            message = "LINEで新規登録が完了しました。"
        else:
            message = "LINEでログインしました。"

        return JsonResponse(
            {
                "ok": True,
                "message": message,
                "redirectUrl": reverse("club:home"),
            }
        )
    except Exception as e:
        return JsonResponse(
            {"ok": False, "message": f"会員処理でエラーが発生しました: {e}"},
            status=400,
        )


@login_required
@require_http_methods(["GET"])
def line_connect(request):
    link = _find_line_link_for_user(request.user)
    link_token = _generate_line_link_token(request.user)

    context = {
        "line_link": link,
        "line_link_token": link_token,
        "manual_form": LineAccountLinkForm(),
        "line_login_enabled": _line_login_enabled(),
        "line_login_url": f"{reverse('club:line_login_start')}?next={urllib.parse.quote(reverse('club:line_connect'))}",
        "line_webhook_full_url": request.build_absolute_uri(reverse("club:line_webhook")),
        "liff_enabled": _liff_enabled(),
    }
    return render(request, "line_connect.html", context)


@login_required
@require_http_methods(["POST"])
def line_link(request):
    action = (request.POST.get("action") or "").strip()

    if action == "unlink":
        link = _find_line_link_for_user(request.user)
        if link:
            try:
                link.is_active = False
                link.save(update_fields=["is_active"])
                messages.success(request, "LINE連携を解除しました。")
            except Exception as e:
                messages.error(request, f"LINE連携の解除に失敗しました: {e}")
        else:
            messages.info(request, "解除対象の連携はありません。")
        return redirect("club:line_connect")

    form = LineAccountLinkForm(request.POST)
    if form.is_valid():
        line_user_id = form.cleaned_data.get("line_user_id")
        is_active = form.cleaned_data.get("is_active", True)

        try:
            conflict = LineAccountLink.objects.filter(line_user_id=line_user_id).exclude(user=request.user).first()
            if conflict:
                messages.error(request, "その line_user_id は別の会員に連携済みです。")
                return redirect("club:line_connect")

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

            try:
                conflict = LineAccountLink.objects.filter(line_user_id=line_user_id).exclude(user=user).first()
                if conflict:
                    if reply_token:
                        send_line_reply(
                            reply_token,
                            "このLINEアカウントは別の会員に連携済みです。",
                        )
                    continue

                LineAccountLink.objects.update_or_create(
                    user=user,
                    defaults={
                        "line_user_id": line_user_id,
                        "is_active": True,
                        "last_event_at": timezone.now(),
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
                    "友だち追加ありがとうございます。LINE内でかんたんに始める場合は、リッチメニューの予約ボタンから進んでください。通常の会員登録やログインはサイト上からも可能です。",
                )

    return HttpResponse("OK")
