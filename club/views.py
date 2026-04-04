import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponse, JsonResponse
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
    StringingOrderForm,
)
from .models import (
    CoachAvailability,
    CoachExpense,
    Court,
    FixedLesson,
    LineAccountLink,
    Reservation,
    ScheduleSurveyResponse,
    StringingOrder,
    TicketConsumption,
    TicketLedger,
    TicketPurchase,
)
from .notifications import (
    build_pending_request_for_coach_message,
    build_request_approved_for_member_message,
    build_request_rejected_for_member_message,
    notify_user,
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


def _schedule_survey_choice_context():
    return {
        "day_choices": ScheduleSurveyResponse.DAY_CHOICES,
        "weekday_time_slot_choices": ScheduleSurveyResponse.WEEKDAY_TIME_SLOT_CHOICES,
        "weekend_time_slot_choices": ScheduleSurveyResponse.WEEKEND_TIME_SLOT_CHOICES,
        "lesson_type_choices": ScheduleSurveyResponse.LESSON_TYPE_CHOICES,
        "frequency_choices": ScheduleSurveyResponse.FREQUENCY_CHOICES,
    }


def _needs_schedule_survey(user):
    if not user or not user.is_authenticated:
        return False
    if getattr(user, "role", None) != "member":
        return False
    if _needs_profile_completion(user):
        return False
    return not ScheduleSurveyResponse.objects.filter(user=user).exists()


def _require_schedule_survey(request):
    if _needs_schedule_survey(request.user):
        messages.warning(
            request,
            "レッスン希望アンケートが未回答です。1〜2分で終わるので、先にご回答をお願いします。回答内容は今後の開催曜日・時間帯の参考になります。"
        )
        return redirect("club:schedule_survey")
    return None


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


def _default_request_end_at(start_value, lesson_type):
    if not start_value:
        return None

    if lesson_type == Reservation.LESSON_GENERAL:
        return start_value + timedelta(hours=2)

    return start_value + timedelta(hours=1)


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
    if getattr(reservation, "substitute_coach_id", None) == user.pk:
        return True
    return False


def _coach_can_manage_request(user, reservation):
    if not user or not user.is_authenticated:
        return False
    if _is_staff_like(user):
        return True
    if not _is_coach_user(user):
        return False
    return reservation.coach_id == user.pk or getattr(reservation, "substitute_coach_id", None) == user.pk


def _is_reservation_canceled(reservation):
    return reservation.status in (Reservation.STATUS_CANCELED, Reservation.STATUS_RAIN_CANCELED)


def _can_user_cancel_reservation(user, reservation):
    if not _user_can_access_reservation(user, reservation):
        return False, "この予約を操作する権限がありません。"

    if _is_reservation_canceled(reservation):
        return False, "この予約はすでにキャンセル済みです。"

    if _is_staff_like(user) or reservation.coach_id == getattr(user, "pk", None) or getattr(
        reservation, "substitute_coach_id", None
    ) == getattr(user, "pk", None):
        return True, ""

    active_count = reservation.active_count_in_same_slot()
    if active_count <= 1 and reservation.status == Reservation.STATUS_ACTIVE:
        return False, "最後の1名となるため、この予約はキャンセルできません。"

    return True, ""


def _lesson_type_label(lesson_type):
    mapping = {
        Reservation.LESSON_GENERAL: "一般レッスン",
        Reservation.LESSON_PRIVATE: "プライベートレッスン",
        Reservation.LESSON_GROUP: "グループレッスン",
        Reservation.LESSON_EVENT: "イベント",
    }
    return mapping.get(lesson_type, lesson_type)


def _month_start_end(target_year: int, target_month: int):
    month_start = date(target_year, target_month, 1)
    if target_month == 12:
        next_month = date(target_year + 1, 1, 1)
    else:
        next_month = date(target_year, target_month + 1, 1)
    return month_start, next_month


def _week_range_for_display(base_date=None):
    target = base_date or timezone.localdate()
    week_start = target - timedelta(days=target.weekday())
    week_end = week_start + timedelta(days=6)
    return week_start, week_end


def _assigned_coach_for_reservation(reservation):
    return reservation.substitute_coach or reservation.coach


def _assigned_coach_id_for_reservation(reservation):
    coach = _assigned_coach_for_reservation(reservation)
    return getattr(coach, "pk", None)


def _slot_key(lesson_type, coach_id, court_id, start_at, end_at):
    return (
        str(lesson_type or ""),
        str(coach_id or ""),
        str(court_id or ""),
        _to_event_datetime_str(start_at) or "",
        _to_event_datetime_str(end_at) or "",
    )


def _pick_request_slot(selected_coach_id, lesson_type, start_at, end_at):
    qs = (
        CoachAvailability.objects.filter(
            lesson_type=lesson_type,
            start_at=start_at,
            end_at=end_at,
        )
        .select_related("coach", "substitute_coach", "court")
        .order_by("coach__username", "coach__id", "id")
    )
    if selected_coach_id:
        qs = qs.filter(coach_id=selected_coach_id)
    return qs.first()


def _assign_pending_request_targets(reservation, selected_coach_id):
    User = get_user_model()

    coach_qs = User.objects.filter(role="coach").order_by("username", "id")
    court_qs = Court.objects.filter(is_active=True).order_by("name", "id")

    selected_coach = None
    if selected_coach_id:
        selected_coach = coach_qs.filter(pk=selected_coach_id).first()
        if not selected_coach:
            raise ValidationError("選択されたコーチが見つかりません。")

    matched_slot = _pick_request_slot(
        selected_coach_id=selected_coach_id,
        lesson_type=reservation.lesson_type,
        start_at=reservation.start_at,
        end_at=reservation.end_at,
    )

    if matched_slot:
        reservation.coach = matched_slot.coach
        reservation.substitute_coach = matched_slot.substitute_coach
        reservation.court = matched_slot.court
        reservation.availability = matched_slot
        reservation.target_level = matched_slot.target_level
        reservation.custom_ticket_price = matched_slot.custom_ticket_price
        reservation.custom_duration_hours = matched_slot.custom_duration_hours
    else:
        fallback_coach = selected_coach or coach_qs.first()
        fallback_court = court_qs.first()

        if not fallback_coach:
            raise ValidationError("予約に利用できるコーチが見つかりません。")

        if not fallback_court:
            raise ValidationError("予約に利用できるコートが見つかりません。")

        reservation.coach = fallback_coach
        reservation.substitute_coach = None
        reservation.court = fallback_court
        reservation.availability = None
        reservation.custom_ticket_price = 0
        reservation.custom_duration_hours = 0

    reservation.requested_court_type = Court.COURT_SONO
    if not selected_coach_id:
        reservation.requested_court_note = "コーチおまかせ"
    else:
        reservation.requested_court_note = ""



def _count_cross_slots_for_responses(responses):
    day_choices = list(ScheduleSurveyResponse.DAY_CHOICES)
    weekday_slot_choices = list(ScheduleSurveyResponse.WEEKDAY_TIME_SLOT_CHOICES)
    weekend_slot_choices = list(ScheduleSurveyResponse.WEEKEND_TIME_SLOT_CHOICES)

    weekday_day_values = {
        ScheduleSurveyResponse.DAY_MON,
        ScheduleSurveyResponse.DAY_TUE,
        ScheduleSurveyResponse.DAY_WED,
        ScheduleSurveyResponse.DAY_THU,
        ScheduleSurveyResponse.DAY_FRI,
    }
    weekend_day_values = {
        ScheduleSurveyResponse.DAY_SAT,
        ScheduleSurveyResponse.DAY_SUN,
    }

    cross_matrix = {}
    for day_value, _day_label in day_choices:
        if day_value in weekday_day_values:
            cross_matrix[day_value] = {slot_value: 0 for slot_value, _label in weekday_slot_choices}
        else:
            cross_matrix[day_value] = {slot_value: 0 for slot_value, _label in weekend_slot_choices}

    for response in responses:
        selected_days = list(response.selected_days or [])
        selected_weekday_slots = list(response.selected_weekday_time_slots or [])
        selected_weekend_slots = list(response.selected_weekend_time_slots or [])

        for day_value in selected_days:
            if day_value in weekday_day_values:
                for slot_value in selected_weekday_slots:
                    if slot_value in cross_matrix.get(day_value, {}):
                        cross_matrix[day_value][slot_value] += 1
            elif day_value in weekend_day_values:
                for slot_value in selected_weekend_slots:
                    if slot_value in cross_matrix.get(day_value, {}):
                        cross_matrix[day_value][slot_value] += 1

    return cross_matrix


def _rank_rows(rows):
    ranked_rows = []
    last_count = None
    current_rank = 0

    for index, row in enumerate(rows, start=1):
        count = int(row.get("count", 0))
        if last_count is None or count != last_count:
            current_rank = index
            last_count = count
        ranked_row = dict(row)
        ranked_row["rank"] = current_rank
        ranked_rows.append(ranked_row)

    return ranked_rows


def _build_recommended_slot_rows_from_responses(responses):
    day_label_map = ScheduleSurveyResponse.day_label_map()
    weekday_label_map = ScheduleSurveyResponse.weekday_time_slot_label_map()
    weekend_label_map = ScheduleSurveyResponse.weekend_time_slot_label_map()
    cross_matrix = _count_cross_slots_for_responses(responses)

    rows = []
    for day_value, slot_counts in cross_matrix.items():
        for slot_value, count in slot_counts.items():
            if slot_value in weekday_label_map:
                slot_label = weekday_label_map.get(slot_value, slot_value)
            else:
                slot_label = weekend_label_map.get(slot_value, slot_value)

            rows.append(
                {
                    "day_value": day_value,
                    "day_label": day_label_map.get(day_value, day_value),
                    "slot_value": slot_value,
                    "slot_label": slot_label,
                    "count": int(count),
                }
            )

    ranked = sorted(rows, key=lambda row: (-row["count"], row["day_label"], row["slot_label"]))
    return _rank_rows(ranked)


def _build_schedule_survey_home_context(user):
    context = {
        "schedule_survey_response": None,
        "schedule_survey_answered": False,
        "schedule_survey_answered_count": 0,
        "schedule_survey_unanswered_count": 0,
        "schedule_survey_answered_rate": 0,
        "schedule_survey_top_slots": [],
        "schedule_survey_level_top_slots": [],
        "schedule_survey_lesson_type_top_slots": [],
        "schedule_survey_top_lesson_type_rankings": [],
        "schedule_survey_member_help_message": "アンケート回答内容をもとに、今後のレッスン開催曜日・時間帯を調整しています。",
        "schedule_survey_coach_top_lesson_types": [],
    }

    User = get_user_model()
    member_users = list(User.objects.filter(role="member", is_active=True).order_by("id"))
    responses = list(ScheduleSurveyResponse.objects.select_related("user").filter(user__role="member").order_by("-answered_at", "-id"))

    total_members = len(member_users)
    answered_count = len(responses)
    unanswered_count = max(total_members - answered_count, 0)
    answered_rate = round((answered_count / total_members) * 100, 1) if total_members > 0 else 0

    context["schedule_survey_answered_count"] = answered_count
    context["schedule_survey_unanswered_count"] = unanswered_count
    context["schedule_survey_answered_rate"] = answered_rate
    context["schedule_survey_top_slots"] = [row for row in _build_recommended_slot_rows_from_responses(responses) if row["count"] > 0][:5]

    response_map = {response.user_id: response for response in responses}
    if user and getattr(user, "is_authenticated", False):
        context["schedule_survey_response"] = response_map.get(user.pk)
        context["schedule_survey_answered"] = user.pk in response_map

        same_level_responses = [
            response
            for response in responses
            if getattr(getattr(response, "user", None), "member_level", "") == getattr(user, "member_level", "")
        ]
        context["schedule_survey_level_top_slots"] = [row for row in _build_recommended_slot_rows_from_responses(same_level_responses) if row["count"] > 0][:5]

        selected_lesson_types = []
        user_response = response_map.get(user.pk)
        if user_response:
            selected_lesson_types = list(user_response.selected_lesson_types or [])

        lesson_type_label_map = ScheduleSurveyResponse.lesson_type_label_map()
        lesson_type_rows = []
        lesson_type_top_slots = []
        for lesson_type_value in selected_lesson_types:
            filtered_responses = [response for response in responses if lesson_type_value in list(response.selected_lesson_types or [])]
            ranked_slots = [row for row in _build_recommended_slot_rows_from_responses(filtered_responses) if row["count"] > 0]
            if ranked_slots:
                lesson_type_rows.append({
                    "lesson_type_value": lesson_type_value,
                    "lesson_type_label": lesson_type_label_map.get(lesson_type_value, lesson_type_value),
                    "count": len(filtered_responses),
                    "top_slot": ranked_slots[0],
                })
                lesson_type_top_slots.append({
                    "lesson_type_value": lesson_type_value,
                    "lesson_type_label": lesson_type_label_map.get(lesson_type_value, lesson_type_value),
                    "rows": ranked_slots[:3],
                })

        context["schedule_survey_top_lesson_type_rankings"] = lesson_type_rows
        context["schedule_survey_lesson_type_top_slots"] = lesson_type_top_slots
    else:
        lesson_type_label_map = ScheduleSurveyResponse.lesson_type_label_map()
        lesson_type_counts = {}
        for response in responses:
            for lesson_type_value in list(response.selected_lesson_types or []):
                lesson_type_counts.setdefault(lesson_type_value, 0)
                lesson_type_counts[lesson_type_value] += 1

        ranked_lesson_types = sorted(
            [
                {
                    "lesson_type_value": lesson_type_value,
                    "lesson_type_label": lesson_type_label_map.get(lesson_type_value, lesson_type_value),
                    "count": count,
                }
                for lesson_type_value, count in lesson_type_counts.items()
            ],
            key=lambda row: (-row["count"], row["lesson_type_label"]),
        )
        context["schedule_survey_coach_top_lesson_types"] = ranked_lesson_types[:3]

    return context

def _send_line_notification_safely(user, message_text):
    if not user or not message_text:
        return
    try:
        notify_user(user, message_text)
    except Exception:
        pass


def home(request):
    if request.user.is_authenticated:
        if _needs_profile_completion(request.user):
            return redirect("club:profile_complete")

        survey_redirect = _require_schedule_survey(request)
        if survey_redirect:
            return survey_redirect

        _sync_fixed_lessons()

    User = get_user_model()
    coaches = User.objects.filter(role="coach").order_by("username")
    selected_coach = request.GET.get("coach", "")

    survey_home_context = _build_schedule_survey_home_context(request.user if request.user.is_authenticated else None)

    return render(
        request,
        "home.html",
        {
            "coaches": coaches,
            "selected_coach": selected_coach,
            "liff_enabled": _liff_enabled(),
            **survey_home_context,
        },
    )




@login_required
@require_http_methods(["GET", "POST"])
def stringing_order_create(request):
    profile_redirect = _require_profile_completed_for_booking(request)
    if profile_redirect:
        return profile_redirect

    survey_redirect = _require_schedule_survey(request)
    if survey_redirect:
        return survey_redirect

    form = StringingOrderForm(request.POST or None)

    if request.method == "POST":
        if form.is_valid():
            order = form.save(commit=False)
            order.user = request.user
            order.status = StringingOrder.STATUS_REQUESTED
            order.save()

            messages.success(
                request,
                f"ガット貼り依頼を受け付けました。料金は {order.total_price()}円 です。"
            )
            return redirect("club:stringing_order_list")

        messages.error(request, "ガット貼り依頼を保存できませんでした。入力内容をご確認ください。")

    return render(
        request,
        "stringing/create.html",
        {
            "form": form,
            "stringing_base_price": 1200,
            "stringing_delivery_fee": 500,
            "stringing_total_with_delivery": 1700,
        },
    )


@login_required
@require_GET
def stringing_order_list(request):
    survey_redirect = _require_schedule_survey(request)
    if survey_redirect:
        return survey_redirect

    queryset = StringingOrder.objects.select_related("user").all()

    if not _is_staff_like(request.user) and not _is_coach_user(request.user):
        queryset = queryset.filter(user=request.user)

    queryset = queryset.order_by("-created_at", "-id")

    return render(
        request,
        "stringing/list.html",
        {
            "stringing_orders": queryset,
            "stringing_base_price": 1200,
            "stringing_delivery_fee": 500,
        },
    )


@login_required
@require_GET
def tickets_view(request):

    survey_redirect = _require_schedule_survey(request)
    if survey_redirect:
        return survey_redirect

    ledgers = TicketLedger.objects.filter(user=request.user).select_related("reservation", "fixed_lesson")[:30]
    purchases = TicketPurchase.objects.filter(user=request.user).order_by("-purchased_at", "-id")[:30]
    consumptions = (
        TicketConsumption.objects.filter(user=request.user)
        .select_related("reservation", "purchase")
        .order_by("-created_at", "-id")[:30]
    )

    return render(
        request,
        "tickets.html",
        {
            "ticket_ledgers": ledgers,
            "ticket_purchases": purchases,
            "ticket_consumptions": consumptions,
            "single_ticket_price": 4000,
            "set4_ticket_price": 14000,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def schedule_survey_view(request):
    if getattr(request.user, "role", None) != "member":
        return redirect("club:home")

    if _needs_profile_completion(request.user):
        return redirect("club:profile_complete")

    existing_response = ScheduleSurveyResponse.objects.filter(user=request.user).first()
    if existing_response:
        messages.info(request, "アンケートは回答済みです。")
        return redirect("club:home")

    context = _schedule_survey_choice_context()
    form_data = {
        "selected_days": [],
        "selected_weekday_time_slots": [],
        "selected_weekend_time_slots": [],
        "selected_lesson_types": [],
        "preferred_frequency": "",
        "free_comment": "",
    }

    if request.method == "POST":
        form_data = {
            "selected_days": request.POST.getlist("selected_days"),
            "selected_weekday_time_slots": request.POST.getlist("selected_weekday_time_slots"),
            "selected_weekend_time_slots": request.POST.getlist("selected_weekend_time_slots"),
            "selected_lesson_types": request.POST.getlist("selected_lesson_types"),
            "preferred_frequency": (request.POST.get("preferred_frequency") or "").strip(),
            "free_comment": (request.POST.get("free_comment") or "").strip(),
        }

        response = ScheduleSurveyResponse(
            user=request.user,
            selected_days=form_data["selected_days"],
            selected_weekday_time_slots=form_data["selected_weekday_time_slots"],
            selected_weekend_time_slots=form_data["selected_weekend_time_slots"],
            selected_lesson_types=form_data["selected_lesson_types"],
            preferred_frequency=form_data["preferred_frequency"],
            free_comment=form_data["free_comment"],
            answered_at=timezone.now(),
        )

        try:
            response.full_clean()
            response.save()
            messages.success(request, "アンケートの回答を保存しました。ご協力ありがとうございます。今後の開催時間帯の参考にします。")
            return redirect("club:home")
        except ValidationError as e:
            if hasattr(e, "messages"):
                for message_text in e.messages:
                    messages.error(request, message_text)
            else:
                messages.error(request, "アンケートを保存できませんでした。入力内容をご確認ください。")

    return render(
        request,
        "survey/schedule_survey.html",
        {
            **context,
            "form_data": form_data,
        },
    )


@login_required
@require_GET
def coach_schedule_survey_summary(request):
    if not (_is_coach_user(request.user) or _is_staff_like(request.user)):
        return HttpResponse("Forbidden", status=403)

    User = get_user_model()
    member_users = User.objects.filter(role="member", is_active=True).order_by("full_name", "username", "id")
    responses = list(
        ScheduleSurveyResponse.objects.select_related("user").filter(user__role="member").order_by("-answered_at", "-id")
    )

    day_choices = list(ScheduleSurveyResponse.DAY_CHOICES)
    weekday_slot_choices = list(ScheduleSurveyResponse.WEEKDAY_TIME_SLOT_CHOICES)
    weekend_slot_choices = list(ScheduleSurveyResponse.WEEKEND_TIME_SLOT_CHOICES)
    lesson_type_choices = list(ScheduleSurveyResponse.LESSON_TYPE_CHOICES)
    frequency_choices = list(ScheduleSurveyResponse.FREQUENCY_CHOICES)

    weekday_day_values = {
        ScheduleSurveyResponse.DAY_MON,
        ScheduleSurveyResponse.DAY_TUE,
        ScheduleSurveyResponse.DAY_WED,
        ScheduleSurveyResponse.DAY_THU,
        ScheduleSurveyResponse.DAY_FRI,
    }
    weekend_day_values = {
        ScheduleSurveyResponse.DAY_SAT,
        ScheduleSurveyResponse.DAY_SUN,
    }

    day_counts = {value: 0 for value, _label in day_choices}
    weekday_time_slot_counts = {value: 0 for value, _label in weekday_slot_choices}
    weekend_time_slot_counts = {value: 0 for value, _label in weekend_slot_choices}
    lesson_type_counts = {value: 0 for value, _label in lesson_type_choices}
    frequency_counts = {value: 0 for value, _label in frequency_choices}

    cross_matrix = {}
    for day_value, _day_label in day_choices:
        if day_value in weekday_day_values:
            cross_matrix[day_value] = {slot_value: 0 for slot_value, _label in weekday_slot_choices}
        else:
            cross_matrix[day_value] = {slot_value: 0 for slot_value, _label in weekend_slot_choices}

    for response in responses:
        selected_days = list(response.selected_days or [])
        selected_weekday_slots = list(response.selected_weekday_time_slots or [])
        selected_weekend_slots = list(response.selected_weekend_time_slots or [])
        selected_lesson_types = list(response.selected_lesson_types or [])

        for day_value in selected_days:
            if day_value in day_counts:
                day_counts[day_value] += 1

        for slot_value in selected_weekday_slots:
            if slot_value in weekday_time_slot_counts:
                weekday_time_slot_counts[slot_value] += 1

        for slot_value in selected_weekend_slots:
            if slot_value in weekend_time_slot_counts:
                weekend_time_slot_counts[slot_value] += 1

        for lesson_type in selected_lesson_types:
            if lesson_type in lesson_type_counts:
                lesson_type_counts[lesson_type] += 1

        if response.preferred_frequency in frequency_counts:
            frequency_counts[response.preferred_frequency] += 1

        for day_value in selected_days:
            if day_value in weekday_day_values:
                for slot_value in selected_weekday_slots:
                    if slot_value in cross_matrix.get(day_value, {}):
                        cross_matrix[day_value][slot_value] += 1
            elif day_value in weekend_day_values:
                for slot_value in selected_weekend_slots:
                    if slot_value in cross_matrix.get(day_value, {}):
                        cross_matrix[day_value][slot_value] += 1

    day_rows = [
        {
            "value": value,
            "label": label,
            "count": day_counts[value],
        }
        for value, label in day_choices
    ]
    weekday_time_slot_rows = [
        {
            "value": value,
            "label": label,
            "count": weekday_time_slot_counts[value],
        }
        for value, label in weekday_slot_choices
    ]
    weekend_time_slot_rows = [
        {
            "value": value,
            "label": label,
            "count": weekend_time_slot_counts[value],
        }
        for value, label in weekend_slot_choices
    ]
    lesson_type_rows = [
        {
            "value": value,
            "label": label,
            "count": lesson_type_counts[value],
        }
        for value, label in lesson_type_choices
    ]
    frequency_rows = [
        {
            "value": value,
            "label": label,
            "count": frequency_counts[value],
        }
        for value, label in frequency_choices
    ]

    cross_rows = []
    recommended_slot_rows = []
    for day_value, day_label in day_choices:
        if day_value in weekday_day_values:
            slot_choices = weekday_slot_choices
            day_group_label = "平日"
        else:
            slot_choices = weekend_slot_choices
            day_group_label = "土日"

        cells = []
        for slot_value, slot_label in slot_choices:
            count = cross_matrix.get(day_value, {}).get(slot_value, 0)
            cells.append(
                {
                    "slot_value": slot_value,
                    "slot_label": slot_label,
                    "count": count,
                }
            )
            recommended_slot_rows.append(
                {
                    "day_value": day_value,
                    "day_label": day_label,
                    "day_group_label": day_group_label,
                    "slot_value": slot_value,
                    "slot_label": slot_label,
                    "count": count,
                }
            )

        cross_rows.append(
            {
                "day_value": day_value,
                "day_label": day_label,
                "cells": cells,
            }
        )

    def _attach_ranks(rows):
        ranked_rows = []
        last_count = None
        current_rank = 0

        for index, row in enumerate(rows, start=1):
            count = int(row.get("count", 0))
            if last_count is None or count != last_count:
                current_rank = index
                last_count = count
            ranked_row = dict(row)
            ranked_row["rank"] = current_rank
            ranked_rows.append(ranked_row)

        return ranked_rows

    top_day_rows = _attach_ranks(
        sorted(day_rows, key=lambda row: (-row["count"], row["label"]))
    )

    top_weekday_time_slot_rows = _attach_ranks(
        sorted(weekday_time_slot_rows, key=lambda row: (-row["count"], row["label"]))
    )

    top_weekend_time_slot_rows = _attach_ranks(
        sorted(weekend_time_slot_rows, key=lambda row: (-row["count"], row["label"]))
    )

    top_lesson_type_rows = _attach_ranks(
        sorted(lesson_type_rows, key=lambda row: (-row["count"], row["label"]))
    )

    top_frequency_rows = _attach_ranks(
        sorted(frequency_rows, key=lambda row: (-row["count"], row["label"]))
    )

    top_recommended_slot_rows = _attach_ranks(
        sorted(
            recommended_slot_rows,
            key=lambda row: (-row["count"], row["day_label"], row["slot_label"]),
        )
    )

    unanswered_users = list(member_users.filter(schedule_survey_response__isnull=True))
    total_members = member_users.count()
    answered_count = len(responses)
    unanswered_count = len(unanswered_users)
    answered_rate = round((answered_count / total_members) * 100, 1) if total_members > 0 else 0

    latest_responses = responses[:50]

    return render(
        request,
        "coach/schedule_survey_summary.html",
        {
            "total_members": total_members,
            "answered_count": answered_count,
            "unanswered_count": unanswered_count,
            "answered_rate": answered_rate,
            "day_rows": day_rows,
            "weekday_time_slot_rows": weekday_time_slot_rows,
            "weekend_time_slot_rows": weekend_time_slot_rows,
            "lesson_type_rows": lesson_type_rows,
            "frequency_rows": frequency_rows,
            "cross_rows": cross_rows,
            "top_day_rows": top_day_rows[:7],
            "top_weekday_time_slot_rows": top_weekday_time_slot_rows[:6],
            "top_weekend_time_slot_rows": top_weekend_time_slot_rows[:6],
            "top_lesson_type_rows": top_lesson_type_rows[:3],
            "top_frequency_rows": top_frequency_rows[:4],
            "top_recommended_slot_rows": top_recommended_slot_rows[:10],
            "unanswered_users": unanswered_users,
            "latest_responses": latest_responses,
        },
    )

@login_required
@require_GET
def coach_fixed_lesson_weekly(request):
    if not (_is_coach_user(request.user) or _is_staff_like(request.user)):
        return HttpResponse("Forbidden", status=403)

    User = get_user_model()
    coach_queryset = User.objects.filter(role="coach").order_by("full_name", "username", "id")

    if _is_staff_like(request.user) and not _is_coach_user(request.user):
        selected_coach_id = (request.GET.get("coach_id") or "").strip()
        selected_coach = (
            coach_queryset.filter(pk=selected_coach_id).first() if selected_coach_id else coach_queryset.first()
        )
    else:
        selected_coach = request.user
        selected_coach_id = str(request.user.pk)

    week_start, week_end = _week_range_for_display()

    fixed_lessons = []
    fixed_queryset = (
        FixedLesson.objects.filter(is_active=True)
        .select_related("coach", "court")
        .prefetch_related("members")
        .order_by("weekday", "start_hour", "id")
    )

    if selected_coach is not None:
        fixed_queryset = fixed_queryset.filter(coach=selected_coach)

    weekday_labels = dict(FixedLesson.WEEKDAY_CHOICES)

    for fixed in fixed_queryset:
        members = list(fixed.members.all().order_by("full_name", "username", "id"))
        target_date = week_start + timedelta(days=int(fixed.weekday))
        start_at, end_at = fixed._build_datetimes_for_date(target_date)

        slot_availability = (
            CoachAvailability.objects.filter(
                coach=fixed.coach,
                court=fixed.court,
                lesson_type=fixed.lesson_type,
                start_at=start_at,
                end_at=end_at,
            )
            .select_related("substitute_coach")
            .first()
        )

        week_reservations = list(
            Reservation.objects.filter(
                fixed_lesson=fixed,
                start_at=start_at,
                end_at=end_at,
            )
            .select_related("user", "coach", "substitute_coach", "court")
            .order_by("user__full_name", "user__username", "id")
        )

        member_names = [member.display_name() for member in members]
        reservation_names = [reservation.user.display_name() for reservation in week_reservations]
        assigned_coach = slot_availability.substitute_coach if slot_availability and slot_availability.substitute_coach else fixed.coach

        fixed_lessons.append(
            {
                "fixed_lesson": fixed,
                "weekday_label": weekday_labels.get(fixed.weekday, str(fixed.weekday)),
                "target_date": target_date,
                "start_at": start_at,
                "end_at": end_at,
                "assigned_coach_name": _display_name(assigned_coach),
                "normal_coach_name": _display_name(fixed.coach),
                "substitute_coach_name": _display_name(slot_availability.substitute_coach)
                if slot_availability and slot_availability.substitute_coach
                else "",
                "has_substitute": bool(slot_availability and slot_availability.substitute_coach),
                "member_count": len(member_names),
                "member_names": member_names,
                "reservation_count": len(reservation_names),
                "reservation_names": reservation_names,
                "slot_availability": slot_availability,
            }
        )

    return render(
        request,
        "coach/fixed_lesson_weekly.html",
        {
            "coach_options": coach_queryset,
            "selected_coach": selected_coach,
            "selected_coach_id": selected_coach_id,
            "fixed_lessons": fixed_lessons,
            "week_start": week_start,
            "week_end": week_end,
            "week_label": f"{week_start:%Y-%m-%d} 〜 {week_end:%Y-%m-%d}",
            "is_staff_mode": _is_staff_like(request.user) and not _is_coach_user(request.user),
        },
    )


@login_required
@require_GET
def coach_ticket_summary(request):
    if not (_is_coach_user(request.user) or _is_staff_like(request.user)):
        return HttpResponse("Forbidden", status=403)

    User = get_user_model()
    today = timezone.localdate()

    try:
        selected_year = int(request.GET.get("year") or today.year)
    except Exception:
        selected_year = today.year

    try:
        selected_month = int(request.GET.get("month") or today.month)
    except Exception:
        selected_month = today.month

    if selected_month < 1 or selected_month > 12:
        selected_month = today.month

    coach_queryset = User.objects.filter(role="coach").order_by("full_name", "username", "id")
    if _is_staff_like(request.user) and not _is_coach_user(request.user):
        selected_coach_id = (request.GET.get("coach_id") or "").strip()
        selected_coach = (
            coach_queryset.filter(pk=selected_coach_id).first() if selected_coach_id else coach_queryset.first()
        )
    else:
        selected_coach = request.user
        selected_coach_id = str(request.user.pk)

    month_start, next_month = _month_start_end(selected_year, selected_month)

    reservations = []
    total_tickets = 0
    total_amount = 0
    breakdown_map = {}
    lesson_type_map = {}

    reservation_qs = (
        Reservation.objects.filter(
            start_at__date__gte=month_start,
            start_at__date__lt=next_month,
        )
        .select_related("user", "coach", "substitute_coach", "court")
        .prefetch_related("ticket_consumptions__purchase")
        .order_by("start_at", "id")
    )

    if selected_coach:
        filtered_reservations = []
        for reservation in reservation_qs:
            if _assigned_coach_id_for_reservation(reservation) == selected_coach.pk:
                filtered_reservations.append(reservation)

        for reservation in filtered_reservations:
            active_consumptions = (
                reservation.ticket_consumptions.filter(refunded_at__isnull=True)
                .select_related("purchase")
                .order_by("created_at", "id")
            )

            row_breakdown_map = {}
            row_tickets = 0
            row_amount = 0

            for consumption in active_consumptions:
                unit_price = int(consumption.unit_price_snapshot or 0)
                tickets_used = int(consumption.tickets_used or 0)

                row_breakdown_map.setdefault(unit_price, 0)
                row_breakdown_map[unit_price] += tickets_used

                breakdown_map.setdefault(unit_price, 0)
                breakdown_map[unit_price] += tickets_used

                row_tickets += tickets_used
                row_amount += unit_price * tickets_used

            if row_tickets <= 0:
                continue

            lesson_type_map.setdefault(reservation.get_lesson_type_display(), 0)
            lesson_type_map[reservation.get_lesson_type_display()] += row_tickets

            total_tickets += row_tickets
            total_amount += row_amount

            breakdown_items = []
            for unit_price, ticket_count in sorted(row_breakdown_map.items(), key=lambda x: x[0]):
                label = f"{unit_price}円券" if unit_price > 0 else "価格不明券"
                breakdown_items.append(
                    {
                        "unit_price": unit_price,
                        "label": label,
                        "tickets": ticket_count,
                        "amount": unit_price * ticket_count,
                    }
                )

            reservations.append(
                {
                    "reservation": reservation,
                    "tickets": row_tickets,
                    "amount": row_amount,
                    "breakdown_items": breakdown_items,
                    "assigned_coach_name": reservation.assigned_coach_display(),
                    "normal_coach_name": reservation.normal_coach_display(),
                    "substitute_coach_name": _display_name(reservation.substitute_coach)
                    if reservation.substitute_coach
                    else "",
                    "has_substitute": reservation.has_substitute_coach(),
                }
            )

    breakdown_rows = []
    for unit_price, ticket_count in sorted(breakdown_map.items(), key=lambda x: x[0]):
        breakdown_rows.append(
            {
                "unit_price": unit_price,
                "label": f"{unit_price}円券" if unit_price > 0 else "価格不明券",
                "tickets": ticket_count,
                "amount": unit_price * ticket_count,
            }
        )

    lesson_type_rows = []
    for lesson_label, ticket_count in sorted(lesson_type_map.items(), key=lambda x: x[0]):
        lesson_type_rows.append(
            {
                "lesson_label": lesson_label,
                "tickets": ticket_count,
            }
        )

    prev_year = selected_year
    prev_month = selected_month - 1
    if prev_month == 0:
        prev_month = 12
        prev_year -= 1

    next_year = selected_year
    next_month = selected_month + 1
    if next_month == 13:
        next_month = 1
        next_year += 1

    return render(
        request,
        "coach/ticket_summary.html",
        {
            "coach_options": coach_queryset,
            "selected_coach": selected_coach,
            "selected_coach_id": selected_coach_id,
            "selected_year": selected_year,
            "selected_month": selected_month,
            "month_label": f"{selected_year}年{selected_month}月",
            "prev_year": prev_year,
            "prev_month": prev_month,
            "next_year": next_year,
            "next_month": next_month,
            "breakdown_rows": breakdown_rows,
            "lesson_type_rows": lesson_type_rows,
            "reservation_rows": reservations,
            "total_tickets": total_tickets,
            "total_amount": total_amount,
            "is_staff_mode": _is_staff_like(request.user) and not _is_coach_user(request.user),
        },
    )


@login_required
@require_GET
def coach_payroll_summary(request):
    if not (_is_coach_user(request.user) or _is_staff_like(request.user)):
        return HttpResponse("Forbidden", status=403)

    User = get_user_model()
    today = timezone.localdate()

    try:
        selected_year = int(request.GET.get("year") or today.year)
    except Exception:
        selected_year = today.year

    try:
        selected_month = int(request.GET.get("month") or today.month)
    except Exception:
        selected_month = today.month

    if selected_month < 1 or selected_month > 12:
        selected_month = today.month

    coach_queryset = User.objects.filter(role="coach").order_by("full_name", "username", "id")
    if _is_staff_like(request.user) and not _is_coach_user(request.user):
        selected_coach_id = (request.GET.get("coach_id") or "").strip()
        selected_coach = (
            coach_queryset.filter(pk=selected_coach_id).first() if selected_coach_id else coach_queryset.first()
        )
    else:
        selected_coach = request.user
        selected_coach_id = str(request.user.pk)

    month_start, next_month = _month_start_end(selected_year, selected_month)

    expense_qs = CoachExpense.objects.filter(
        expense_date__gte=month_start,
        expense_date__lt=next_month,
    ).order_by("expense_date", "id")

    expense_rows = list(expense_qs)
    total_expense_amount = sum([int(obj.amount or 0) for obj in expense_rows])

    monthly_consumptions = (
        TicketConsumption.objects.filter(
            refunded_at__isnull=True,
            reservation__start_at__date__gte=month_start,
            reservation__start_at__date__lt=next_month,
        )
        .select_related("reservation__coach", "reservation__substitute_coach")
    )

    active_coach_ids = set()
    for consumption in monthly_consumptions:
        reservation = consumption.reservation
        if not reservation:
            continue
        assigned_coach = _assigned_coach_for_reservation(reservation)
        if assigned_coach and getattr(assigned_coach, "role", "") == "coach":
            active_coach_ids.add(assigned_coach.pk)

    active_coach_count = len(active_coach_ids)
    per_coach_expense = int(total_expense_amount / active_coach_count) if active_coach_count > 0 else 0

    total_tickets = 0
    total_amount = 0
    breakdown_rows = []
    reservation_rows = []

    reservation_qs = (
        Reservation.objects.filter(
            start_at__date__gte=month_start,
            start_at__date__lt=next_month,
        )
        .select_related("user", "coach", "substitute_coach", "court")
        .prefetch_related("ticket_consumptions__purchase")
        .order_by("start_at", "id")
    )

    if selected_coach:
        filtered_reservations = []
        for reservation in reservation_qs:
            if _assigned_coach_id_for_reservation(reservation) == selected_coach.pk:
                filtered_reservations.append(reservation)

        breakdown_map = {}
        for reservation in filtered_reservations:
            active_consumptions = (
                reservation.ticket_consumptions.filter(refunded_at__isnull=True)
                .select_related("purchase")
                .order_by("created_at", "id")
            )

            row_breakdown_items = []
            row_breakdown_map = {}
            row_tickets = 0
            row_amount = 0

            for consumption in active_consumptions:
                unit_price = int(consumption.unit_price_snapshot or 0)
                tickets_used = int(consumption.tickets_used or 0)

                breakdown_map.setdefault(unit_price, 0)
                breakdown_map[unit_price] += tickets_used

                row_breakdown_map.setdefault(unit_price, 0)
                row_breakdown_map[unit_price] += tickets_used

                row_tickets += tickets_used
                row_amount += unit_price * tickets_used

            if row_tickets <= 0:
                continue

            total_tickets += row_tickets
            total_amount += row_amount

            for unit_price, tickets in sorted(row_breakdown_map.items(), key=lambda x: x[0]):
                row_breakdown_items.append(
                    {
                        "label": f"{unit_price}円券" if unit_price > 0 else "価格不明券",
                        "tickets": tickets,
                        "amount": unit_price * tickets,
                    }
                )

            reservation_rows.append(
                {
                    "reservation": reservation,
                    "tickets": row_tickets,
                    "amount": row_amount,
                    "breakdown_items": row_breakdown_items,
                    "assigned_coach_name": reservation.assigned_coach_display(),
                    "normal_coach_name": reservation.normal_coach_display(),
                    "substitute_coach_name": _display_name(reservation.substitute_coach)
                    if reservation.substitute_coach
                    else "",
                    "has_substitute": reservation.has_substitute_coach(),
                }
            )

        for unit_price, tickets in sorted(breakdown_map.items(), key=lambda x: x[0]):
            breakdown_rows.append(
                {
                    "label": f"{unit_price}円券" if unit_price > 0 else "価格不明券",
                    "tickets": tickets,
                    "amount": unit_price * tickets,
                }
            )

    estimated_salary = total_amount - per_coach_expense

    category_totals = {}
    for expense in expense_rows:
        label = expense.get_category_display()
        category_totals.setdefault(label, 0)
        category_totals[label] += int(expense.amount or 0)

    category_rows = []
    for label, amount in sorted(category_totals.items(), key=lambda x: x[0]):
        category_rows.append(
            {
                "label": label,
                "amount": amount,
            }
        )

    prev_year = selected_year
    prev_month = selected_month - 1
    if prev_month == 0:
        prev_month = 12
        prev_year -= 1

    next_year = selected_year
    next_month = selected_month + 1
    if next_month == 13:
        next_month = 1
        next_year += 1

    return render(
        request,
        "coach/payroll_summary.html",
        {
            "coach_options": coach_queryset,
            "selected_coach": selected_coach,
            "selected_coach_id": selected_coach_id,
            "selected_year": selected_year,
            "selected_month": selected_month,
            "month_label": f"{selected_year}年{selected_month}月",
            "prev_year": prev_year,
            "prev_month": prev_month,
            "next_year": next_year,
            "next_month": next_month,
            "is_staff_mode": _is_staff_like(request.user) and not _is_coach_user(request.user),
            "breakdown_rows": breakdown_rows,
            "reservation_rows": reservation_rows,
            "expense_rows": expense_rows,
            "category_rows": category_rows,
            "total_tickets": total_tickets,
            "total_amount": total_amount,
            "total_expense_amount": total_expense_amount,
            "active_coach_count": active_coach_count,
            "per_coach_expense": per_coach_expense,
            "estimated_salary": estimated_salary,
        },
    )


@never_cache
@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        if _needs_profile_completion(request.user):
            return redirect("club:profile_complete")
        if _needs_schedule_survey(request.user):
            return redirect("club:schedule_survey")
        return redirect("club:home")

    form = AuthenticationForm(request, data=request.POST or None)

    if request.method == "POST":
        if form.is_valid():
            login(request, form.get_user())
            if _needs_profile_completion(request.user):
                return redirect("club:profile_complete")
            if _needs_schedule_survey(request.user):
                messages.info(request, "ログインありがとうございます。最初にアンケートへご回答ください。")
                return redirect("club:schedule_survey")
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
        if _needs_schedule_survey(request.user):
            return redirect("club:schedule_survey")
        return redirect("club:home")

    form = MemberRegistrationForm(request.POST or None)

    if request.method == "POST":
        if form.is_valid():
            user = form.save()
            _login_user_with_default_backend(request, user)
            messages.success(request, "新規会員登録が完了しました。")
            if _needs_profile_completion(request.user):
                return redirect("club:profile_complete")
            if _needs_schedule_survey(request.user):
                messages.info(request, "最初にレッスン希望アンケートへご回答ください。")
                return redirect("club:schedule_survey")
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
            if _needs_schedule_survey(request.user):
                messages.info(request, "続けてアンケートへご回答ください。")
                return redirect("club:schedule_survey")
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


@require_GET
def help_view(request):
    return render(
        request,
        "help/help.html",
        {
            "help_sections": [
                {
                    "title": "はじめての使い方",
                    "items": [
                        "LINEまたは通常ログインで会員登録を行います。",
                        "初回ログイン後、会員情報の入力とアンケート回答を完了します。",
                        "ホーム画面から予約作成・予約一覧・チケット確認へ進めます。",
                    ],
                },
                {
                    "title": "予約の流れ",
                    "items": [
                        "ホームや予約作成画面から希望日時・レッスン種別を選びます。",
                        "private / group は申請後、コーチ承認で予約成立になります。",
                        "予約一覧では、現在の予約内容確認やキャンセル可否の確認ができます。",
                    ],
                },
                {
                    "title": "チケットの考え方",
                    "items": [
                        "一般レッスンは2時間でチケット1枚です。",
                        "プライベートは1時間ごとにチケット2枚です。",
                        "グループは1時間ごとに参加人数分のチケットを消費します。",
                    ],
                },
            ],
        },
    )


@require_GET
def terms_view(request):
    return render(
        request,
        "terms.html",
        {
            "terms_sections": [
                {
                    "title": "第1条（適用）",
                    "body": [
                        "本利用規約は、Play Design Tennis が提供する予約・チケット・LINE連携等のサービス利用条件を定めるものです。",
                        "会員および利用者は、本サービスを利用した時点で本規約に同意したものとみなします。",
                    ],
                },
                {
                    "title": "第2条（会員登録）",
                    "body": [
                        "会員登録時には、正確かつ最新の情報を登録してください。",
                        "登録情報に変更があった場合は、速やかに会員情報を更新してください。",
                    ],
                },
                {
                    "title": "第3条（予約・申請）",
                    "body": [
                        "予約作成画面から希望日時・種別を選択し、必要に応じてコーチ承認を経て予約が成立します。",
                        "private / group レッスンは、コーチ承認前は申請中の扱いとなります。",
                        "運営上必要な場合、予約内容の変更・調整をお願いすることがあります。",
                    ],
                },
                {
                    "title": "第4条（チケット）",
                    "body": [
                        "チケットの消費ルールは、一般レッスン2時間で1枚、プライベート1時間ごとに2枚、グループ1時間ごとに参加人数分です。",
                        "購入済みチケットの返金可否や有効性については、運営の定める運用に従います。",
                    ],
                },
                {
                    "title": "第5条（禁止事項）",
                    "body": [
                        "虚偽情報による登録、他者への迷惑行為、不正アクセス、営利目的での無断利用を禁止します。",
                        "サービス運営を妨げる行為が確認された場合、利用停止等の措置を取ることがあります。",
                    ],
                },
                {
                    "title": "第6条（免責・変更）",
                    "body": [
                        "天候、設備状況、運営都合等により、レッスン内容や時間の変更・中止が発生する場合があります。",
                        "本規約およびサービス内容は、必要に応じて変更されることがあります。",
                    ],
                },
            ],
        },
    )


@login_required
@require_GET
def calendar_events(request):
    _sync_fixed_lessons()

    events = []
    coach_filter = (request.GET.get("coach") or request.GET.get("coach_id") or "").strip()
    start_filter = _parse_query_datetime(request.GET.get("start"))
    end_filter = _parse_query_datetime(request.GET.get("end"))

    availability_qs = CoachAvailability.objects.select_related("coach", "substitute_coach", "court").all()
    reservation_qs = (
        Reservation.objects.select_related("user", "coach", "substitute_coach", "court", "availability")
        .prefetch_related("ticket_consumptions__purchase")
        .exclude(status__in=[Reservation.STATUS_CANCELED, Reservation.STATUS_RAIN_CANCELED])
    )

    if coach_filter:
        availability_qs = availability_qs.filter(coach_id=coach_filter)

    if start_filter:
        availability_qs = availability_qs.filter(end_at__gt=start_filter)
        reservation_qs = reservation_qs.filter(end_at__gt=start_filter)

    if end_filter:
        availability_qs = availability_qs.filter(start_at__lt=end_filter)
        reservation_qs = reservation_qs.filter(start_at__lt=end_filter)

    availability_list = list(availability_qs.order_by("start_at", "coach_id", "court_id", "id"))
    reservation_list = list(reservation_qs)

    if coach_filter:
        reservation_list = [
            reservation
            for reservation in reservation_list
            if str(_assigned_coach_id_for_reservation(reservation) or "") == str(coach_filter)
        ]

    active_slot_counts = {}
    slot_capacity_map = {}
    my_active_slot_keys = set()

    for availability in availability_list:
        slot_key = _slot_key(
            lesson_type=availability.lesson_type,
            coach_id=availability.coach_id,
            court_id=availability.court_id,
            start_at=availability.start_at,
            end_at=availability.end_at,
        )
        slot_capacity_map[slot_key] = int(availability.capacity or 0)

    for reservation in reservation_list:
        slot_key = _slot_key(
            lesson_type=reservation.lesson_type,
            coach_id=reservation.coach_id,
            court_id=reservation.court_id,
            start_at=reservation.start_at,
            end_at=reservation.end_at,
        )
        if reservation.status == Reservation.STATUS_ACTIVE:
            active_slot_counts.setdefault(slot_key, 0)
            active_slot_counts[slot_key] += 1
            if reservation.user_id == request.user.pk:
                my_active_slot_keys.add(slot_key)

    active_slot_keys = set(active_slot_counts.keys())

    for obj in availability_list:
        slot_key = _slot_key(
            lesson_type=obj.lesson_type,
            coach_id=obj.coach_id,
            court_id=obj.court_id,
            start_at=obj.start_at,
            end_at=obj.end_at,
        )
        if slot_key in active_slot_keys:
            continue

        coach = obj.coach
        court = obj.court

        if obj.lesson_type == Reservation.LESSON_GENERAL:
            title_text = "一般レッスン"
        else:
            title_text = "受付中"

        query = urlencode(
            {
                "coach": getattr(coach, "pk", "") or "",
                "lesson_type": obj.lesson_type,
                "start": _to_event_datetime_str(obj.start_at) or "",
                "end": _to_event_datetime_str(obj.end_at) or "",
            }
        )

        events.append(
            {
                "id": f"availability-{obj.pk}",
                "title": title_text,
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
                    "substitute_coach_name": _display_name(obj.substitute_coach) if obj.substitute_coach else "",
                    "court": str(court),
                    "lesson_type_display": _lesson_type_label(obj.lesson_type),
                    "capacity": obj.capacity,
                    "coach_count": obj.coach_count,
                    "court_count": obj.court_count,
                    "target_level_display": obj.get_target_level_display(),
                    "reserve_url": f"{reverse('club:reservation_create')}?{query}",
                },
            }
        )

    for obj in reservation_list:
        is_mine = bool(obj.user_id == request.user.pk)
        slot_key = _slot_key(
            lesson_type=obj.lesson_type,
            coach_id=obj.coach_id,
            court_id=obj.court_id,
            start_at=obj.start_at,
            end_at=obj.end_at,
        )

        if obj.status == Reservation.STATUS_ACTIVE and not is_mine and slot_key in my_active_slot_keys:
            continue

        active_count = int(active_slot_counts.get(slot_key, 0))
        capacity = int(
            getattr(getattr(obj, "availability", None), "capacity", 0)
            or slot_capacity_map.get(slot_key, 0)
            or max(active_count, 1)
        )

        can_cancel, cancel_reason = _can_user_cancel_reservation(request.user, obj)
        cancel_url = reverse("club:reservation_cancel", kwargs={"pk": obj.pk}) if can_cancel else ""

        if obj.status == Reservation.STATUS_PENDING:
            event_title = f"申請中 {active_count}/{capacity}"
            background_color = "#f59e0b"
        elif is_mine:
            event_title = f"あなたの予約 {active_count}/{capacity}"
            background_color = "#3b82f6"
        else:
            event_title = f"予約済み {active_count}/{capacity}"
            background_color = "#ef4444"

        assigned_coach = _assigned_coach_for_reservation(obj)

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
                    "coach_name": _display_name(assigned_coach),
                    "normal_coach_name": _display_name(obj.coach),
                    "substitute_coach_name": _display_name(obj.substitute_coach) if obj.substitute_coach else "",
                    "has_substitute": obj.has_substitute_coach(),
                    "court": str(obj.court),
                    "lesson_type_display": _lesson_type_label(obj.lesson_type),
                    "tickets_used": obj.tickets_used,
                    "ticket_breakdown_text": obj.ticket_breakdown_text(),
                    "is_canceled": False,
                    "is_mine": is_mine,
                    "can_cancel": can_cancel,
                    "cancel_url": cancel_url,
                    "cancel_reason": cancel_reason,
                    "status_display": obj.get_status_display(),
                    "participant_count": active_count,
                    "capacity": capacity,
                    "participant_summary": f"{active_count}/{capacity}",
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

    survey_redirect = _require_schedule_survey(request)
    if survey_redirect:
        return survey_redirect

    _sync_fixed_lessons()

    initial = {}
    coach_id = (request.GET.get("coach") or "").strip()
    lesson_type = request.GET.get("lesson_type") or Reservation.LESSON_PRIVATE
    if lesson_type not in (Reservation.LESSON_PRIVATE, Reservation.LESSON_GROUP):
        lesson_type = Reservation.LESSON_PRIVATE

    start_value = _parse_query_datetime(request.GET.get("start"))
    end_value = _parse_query_datetime(request.GET.get("end"))

    if not end_value and start_value:
        end_value = _default_request_end_at(start_value, lesson_type)

    if coach_id:
        initial["coach_choice"] = coach_id
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
                    reservation.status = Reservation.STATUS_PENDING

                    selected_coach_id = form.cleaned_data.get("coach_choice")
                    _assign_pending_request_targets(reservation, selected_coach_id)

                    reservation.full_clean()
                    reservation.save()

                coach_message = build_pending_request_for_coach_message(reservation)
                _send_line_notification_safely(reservation.coach, coach_message)
                if getattr(reservation, "substitute_coach_id", None):
                    _send_line_notification_safely(reservation.substitute_coach, coach_message)

                messages.success(request, "申請を送信しました。コーチ承認後に成立します。")
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
    survey_redirect = _require_schedule_survey(request)
    if survey_redirect:
        return survey_redirect

    _sync_fixed_lessons()

    qs = (
        Reservation.objects.select_related("user", "coach", "substitute_coach", "court")
        .prefetch_related("ticket_consumptions__purchase")
        .all()
    )

    if _is_staff_like(request.user):
        pass
    elif _is_coach_user(request.user):
        filtered_qs = []
        for reservation in qs:
            if reservation.coach_id == request.user.pk or getattr(reservation, "substitute_coach_id", None) == request.user.pk:
                filtered_qs.append(reservation.pk)
        qs = qs.filter(pk__in=filtered_qs)
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
                "assigned_coach_name": reservation.assigned_coach_display(),
                "normal_coach_name": reservation.normal_coach_display(),
                "substitute_coach_name": _display_name(reservation.substitute_coach) if reservation.substitute_coach else "",
                "has_substitute": reservation.has_substitute_coach(),
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

    messages.success(request, "予約をキャンセルしました。")
    return redirect("club:reservation_list")


@login_required
@require_GET
def coach_availability_list(request):
    _sync_fixed_lessons()

    qs = CoachAvailability.objects.select_related("coach", "substitute_coach", "court").all()

    if _is_coach_user(request.user):
        qs = qs.filter(coach=request.user)

    qs = qs.order_by("start_at")

    pending_qs = (
        Reservation.objects.select_related("user", "coach", "substitute_coach", "court")
        .filter(
            status=Reservation.STATUS_PENDING,
            lesson_type__in=[Reservation.LESSON_PRIVATE, Reservation.LESSON_GROUP],
        )
        .order_by("start_at", "created_at", "id")
    )

    if _is_staff_like(request.user) and not _is_coach_user(request.user):
        pending_reservations = list(pending_qs)
    elif _is_coach_user(request.user):
        pending_reservations = [
            reservation
            for reservation in pending_qs
            if reservation.coach_id == request.user.pk or getattr(reservation, "substitute_coach_id", None) == request.user.pk
        ]
    else:
        return HttpResponse("Forbidden", status=403)

    return render(
        request,
        "coach/availability_list.html",
        {
            "availabilities": qs,
            "pending_reservations": pending_reservations,
        },
    )


@login_required
@require_POST
def coach_request_approve(request, pk):
    reservation = get_object_or_404(
        Reservation.objects.select_related("user", "coach", "substitute_coach", "court"),
        pk=pk,
    )

    if not _coach_can_manage_request(request.user, reservation):
        return HttpResponse("Forbidden", status=403)

    if reservation.status != Reservation.STATUS_PENDING:
        messages.error(request, "この申請はすでに処理済みです。")
        return redirect("club:coach_availability_list")

    if reservation.lesson_type not in (Reservation.LESSON_PRIVATE, Reservation.LESSON_GROUP):
        messages.error(request, "この申請は承認対象外です。")
        return redirect("club:coach_availability_list")

    try:
        with transaction.atomic():
            reservation.activate_after_approval(created_by=request.user)

        member_message = build_request_approved_for_member_message(reservation)
        _send_line_notification_safely(reservation.user, member_message)

        messages.success(
            request,
            f"申請を承認しました。会員: {_display_name(reservation.user)} / {_lesson_type_label(reservation.lesson_type)}",
        )
    except ValidationError as e:
        messages.error(request, str(e))
    except Exception as e:
        messages.error(request, f"申請の承認に失敗しました: {e}")

    return redirect("club:coach_availability_list")


@login_required
@require_POST
def coach_request_reject(request, pk):
    reservation = get_object_or_404(
        Reservation.objects.select_related("user", "coach", "substitute_coach", "court"),
        pk=pk,
    )

    if not _coach_can_manage_request(request.user, reservation):
        return HttpResponse("Forbidden", status=403)

    if reservation.status != Reservation.STATUS_PENDING:
        messages.error(request, "この申請はすでに処理済みです。")
        return redirect("club:coach_availability_list")

    if reservation.lesson_type not in (Reservation.LESSON_PRIVATE, Reservation.LESSON_GROUP):
        messages.error(request, "この申請は却下対象外です。")
        return redirect("club:coach_availability_list")

    try:
        with transaction.atomic():
            reservation.reject_request(
                created_by=request.user,
                reason="コーチ却下",
            )

        member_message = build_request_rejected_for_member_message(reservation)
        _send_line_notification_safely(reservation.user, member_message)

        messages.success(
            request,
            f"申請を却下しました。会員: {_display_name(reservation.user)} / {_lesson_type_label(reservation.lesson_type)}",
        )
    except ValidationError as e:
        messages.error(request, str(e))
    except Exception as e:
        messages.error(request, f"申請の却下に失敗しました: {e}")

    return redirect("club:coach_availability_list")


@login_required
@require_http_methods(["GET", "POST"])
def coach_availability_create(request, pk=None):
    instance = None
    if pk is not None:
        instance = get_object_or_404(CoachAvailability, pk=pk)
        if not _is_staff_like(request.user) and instance.coach != request.user:
            return HttpResponse("Forbidden", status=403)

    form = CoachAvailabilityForm(
        request.POST or None,
        request_user=request.user,
        instance=instance,
    )

    if request.method == "POST":
        if form.is_valid():
            availability = form.save(commit=False)
            if _is_coach_user(request.user) and not _is_staff_like(request.user):
                availability.coach = request.user
            availability.save()

            if instance is None:
                messages.success(request, "コーチスケジュールを登録しました。")
            else:
                messages.success(request, "コーチスケジュールを更新しました。")
            return redirect("club:coach_availability_list")

        messages.error(request, "コーチスケジュールを保存できませんでした。入力内容をご確認ください。")

    return render(
        request,
        "coach/availability_create.html",
        {
            "form": form,
            "is_edit": instance is not None,
            "availability": instance,
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
    messages.success(request, "コーチスケジュールを削除しました。")
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

        if _needs_schedule_survey(user):
            messages.info(request, "初回ログインありがとうございます。レッスン希望アンケートに回答していただくと、今後の開催時間帯の参考になります。")
            return redirect("club:schedule_survey")

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

        if _needs_schedule_survey(user):
            return JsonResponse(
                {
                    "ok": True,
                    "message": "レッスン希望アンケートへの回答をお願いします。1〜2分で完了します。",
                    "redirectUrl": reverse("club:schedule_survey"),
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
    survey_redirect = _require_schedule_survey(request)
    if survey_redirect:
        return survey_redirect

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
                continue

            try:
                conflict = LineAccountLink.objects.filter(line_user_id=line_user_id).exclude(user=user).first()
                if conflict:
                    continue

                LineAccountLink.objects.update_or_create(
                    user=user,
                    defaults={
                        "line_user_id": line_user_id,
                        "is_active": True,
                        "last_event_at": timezone.now(),
                    },
                )
            except Exception:
                pass

    return HttpResponse("OK")
