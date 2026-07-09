import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
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
    LessonWaitlist,
    LineAccountLink,
    Reservation,
    ScheduleSurveyResponse,
    ShopEstimateRequest,
    ShopProductMaster,
    StringingOrder,
    TicketConsumption,
    TicketLedger,
    TicketPurchase,
    PREOPEN_CASH_PRICE,
    is_preopen_cash_lesson_date,
)
from .family_reservations import (
    build_participant_choices_for_user,
    resolve_reservation_participant,
    save_reservation_participant_snapshot,
    validate_participant_can_book_lesson,
)
from .notifications import (
    build_pending_request_for_coach_message,
    build_request_approved_for_member_message,
    build_request_rejected_for_member_message,
    build_reservation_rain_canceled_message,
    build_stringing_order_created_for_coach_message,
    build_reservation_created_message,
    build_waitlist_registered_for_member_email_message,
    notify_user_email_only,
    notify_user_line_only,
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
    return getattr(user, "role", None) in ("coach", "contractor_coach")


def _can_user_take_lessons(user):
    if not user or not user.is_authenticated:
        return False
    return getattr(user, "role", None) in ("member", "contractor_coach")


def _schedule_survey_choice_context():
    return {
        "day_choices": ScheduleSurveyResponse.DAY_CHOICES,
        "weekday_time_slot_choices": ScheduleSurveyResponse.WEEKDAY_TIME_SLOT_CHOICES,
        "weekend_time_slot_choices": ScheduleSurveyResponse.WEEKEND_TIME_SLOT_CHOICES,
        "lesson_type_choices": ScheduleSurveyResponse.LESSON_TYPE_CHOICES,
        "frequency_choices": ScheduleSurveyResponse.FREQUENCY_CHOICES,
    }


def _needs_schedule_survey(user):
    # アンケート機能は役割を終えたため、画面上は非表示・強制遷移なしにする。
    # 既存データと集計画面は残し、必要になった場合だけ管理側で再利用できるようにする。
    return False


def _require_schedule_survey(request):
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


def _lesson_calendar_landing_url():
    today = timezone.localdate()
    campaign_start = date(2026, 5, 25)
    campaign_end = date(2026, 7, 31)

    if campaign_start <= today <= campaign_end:
        target_year = 2026
        target_month = 7
    else:
        target_year = today.year
        target_month = today.month

    return f"{reverse('club:lesson_calendar')}?{urlencode({'year': target_year, 'month': target_month})}"


def _normalize_next_url(value):
    default_landing_url = _lesson_calendar_landing_url()

    if not value:
        return default_landing_url

    value = str(value).strip()
    if not value.startswith("/"):
        return default_landing_url
    if value.startswith("//"):
        return default_landing_url

    # LINEログイン後は、通常の戻り先をレッスンカレンダーに統一する。
    # 2026/5/25〜2026/7/31 は 2026年7月のレッスンカレンダーへ送る。
    # /line/link/ 経由の「LINEで登録・ログイン」ボタンでも、ログイン完了後にLINE連携タブへ戻さない。
    standard_redirects = {
        "/",
        reverse("club:home"),
        reverse("club:lesson_calendar"),
        reverse("club:line_connect"),
        reverse("club:login"),
    }
    if value in standard_redirects:
        return default_landing_url

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


def _is_preopen_cash_regular_lesson(lesson_type, start_at):
    return lesson_type == Reservation.LESSON_GENERAL and is_preopen_cash_lesson_date(start_at)


def _regular_lesson_payment_label(lesson_type, start_at):
    if _is_preopen_cash_regular_lesson(lesson_type, start_at):
        return f"7月プレオープン：当日、受付時に{PREOPEN_CASH_PRICE:,}円のお支払いをお願いします（チケットは使いません）"
    return "1レッスン＝チケット1枚"


def _regular_lesson_confirm_note(lesson_type, start_at):
    if _is_preopen_cash_regular_lesson(lesson_type, start_at):
        return "7月のプレオープン期間中は、チケットを使わずにご参加いただけます。当日は受付時に参加費のお支払いをお願いします。"
    return "通常レッスンは予約確定時にチケットを消費します。定員に達した場合は受付終了になります。"


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


def _lesson_level_values(obj):
    if not obj:
        return []
    first_level = getattr(obj, "target_level", "") or ""
    second_level = getattr(obj, "target_level_2", "") or ""
    values = []
    if first_level:
        values.append(first_level)
    if second_level and second_level != first_level:
        values.append(second_level)
    return values


def _lesson_level_label(obj):
    if hasattr(obj, "target_level_display_label"):
        try:
            return obj.target_level_display_label()
        except Exception:
            pass

    labels = []
    UserModel = get_user_model()
    level_map = dict(getattr(UserModel, "LEVEL_CHOICES", []))
    for level_value in _lesson_level_values(obj):
        label = level_map.get(level_value, level_value)
        if label and label not in labels:
            labels.append(label)
    return "・".join(labels)


def _user_can_book_lesson_levels(user, obj):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    levels = _lesson_level_values(obj)
    if not levels:
        return True
    if hasattr(user, "can_book_any_level"):
        return user.can_book_any_level(*levels)
    if hasattr(user, "can_book_level"):
        return any(user.can_book_level(level) for level in levels)
    return True


def _slot_level_allowed(user, target_level, target_level_2=""):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if hasattr(user, "can_book_any_level"):
        return user.can_book_any_level(target_level, target_level_2)
    if hasattr(user, "can_book_level"):
        levels = [level for level in [target_level, target_level_2] if level]
        if not levels:
            return True
        return any(user.can_book_level(level) for level in levels)
    return True


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

    coach_qs = User.objects.filter(role__in=("coach", "contractor_coach")).order_by("username", "id")
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
        reservation.target_level_2 = getattr(matched_slot, "target_level_2", "") or ""
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

def _send_line_notification_safely(user, message_text, subject="Play Design Tennis 通知"):
    """
    LINE通知専用。
    月200通の無料枠を節約するため、呼び出し箇所は雨天中止とキャンセル待ち空き通知に限定します。
    """
    if not user or not message_text:
        return
    try:
        notify_user_line_only(user, message_text, subject=subject)
    except Exception:
        pass


def _send_email_notification_safely(user, subject, message_text):
    """
    メール通知専用。
    予約完了・予約キャンセル・キャンセル待ち登録・個別レッスン申請系はこちらを使います。
    """
    if not user or not message_text:
        return
    try:
        notify_user_email_only(user, message_text, subject=subject)
    except Exception:
        pass

def _lesson_waitlist_lesson_label(waitlist_or_reservation):
    try:
        start_local = timezone.localtime(waitlist_or_reservation.start_at)
    except Exception:
        start_local = waitlist_or_reservation.start_at

    try:
        end_local = timezone.localtime(waitlist_or_reservation.end_at)
    except Exception:
        end_local = waitlist_or_reservation.end_at

    try:
        lesson_label = waitlist_or_reservation.get_lesson_type_display()
    except Exception:
        lesson_label = _lesson_type_label(getattr(waitlist_or_reservation, "lesson_type", ""))

    try:
        level_label = _lesson_level_label(waitlist_or_reservation) or waitlist_or_reservation.get_target_level_display()
    except Exception:
        level_label = getattr(waitlist_or_reservation, "target_level", "-")

    try:
        coach_name = waitlist_or_reservation.assigned_coach_display()
    except Exception:
        coach_name = _display_name(
            getattr(waitlist_or_reservation, "substitute_coach", None)
            or getattr(waitlist_or_reservation, "coach", None)
        )

    court_name = str(getattr(waitlist_or_reservation, "court", "") or "未定")

    return {
        "date": f"{start_local:%Y/%m/%d}",
        "time": f"{start_local:%H:%M}〜{end_local:%H:%M}",
        "lesson": lesson_label,
        "level": level_label,
        "coach": coach_name,
        "court": court_name,
    }


def _build_waitlist_registered_for_member_message(waitlist):
    label = _lesson_waitlist_lesson_label(waitlist)
    return (
        "キャンセル待ち登録が完了しました。\n\n"
        f"日時：{label['date']} {label['time']}\n"
        f"レッスン：{label['lesson']}\n"
        f"レベル：{label['level']}\n"
        f"コーチ：{label['coach']}\n"
        f"コート：{label['court']}\n\n"
        "空きが出た場合はご案内します。"
    )


def _build_waitlist_registered_for_coach_message(waitlist):
    label = _lesson_waitlist_lesson_label(waitlist)
    return (
        "キャンセル待ちが入りました。\n\n"
        f"会員：{_display_name(waitlist.user)}\n"
        f"日時：{label['date']} {label['time']}\n"
        f"レッスン：{label['lesson']}\n"
        f"レベル：{label['level']}\n"
        f"コート：{label['court']}"
    )


def _build_waitlist_canceled_for_member_message(waitlist):
    label = _lesson_waitlist_lesson_label(waitlist)
    return (
        "キャンセル待ちを取り消しました。\n\n"
        f"日時：{label['date']} {label['time']}\n"
        f"レッスン：{label['lesson']}\n"
        f"コーチ：{label['coach']}"
    )


def _build_waitlist_opening_for_member_message(waitlist):
    label = _lesson_waitlist_lesson_label(waitlist)
    reserve_url = reverse("club:reservation_create")
    query = {}
    if waitlist.availability_id:
        query["availability_id"] = waitlist.availability_id
    elif waitlist.fixed_lesson_id:
        query["fixed_lesson_id"] = waitlist.fixed_lesson_id
        query["lesson_date"] = waitlist.start_at.date().isoformat()

    if query:
        reserve_url = f"{reserve_url}?{urlencode(query)}"

    return (
        "キャンセル待ち中のレッスンに空きが出ました。\n\n"
        f"日時：{label['date']} {label['time']}\n"
        f"レッスン：{label['lesson']}\n"
        f"レベル：{label['level']}\n"
        f"コーチ：{label['coach']}\n\n"
        "予約は先着順です。レッスンカレンダー、または予約画面からお手続きください。\n"
        f"{reserve_url}"
    )


def _waitlist_slot_key_from_obj(obj):
    return _slot_key(
        lesson_type=getattr(obj, "lesson_type", ""),
        coach_id=getattr(obj, "coach_id", None),
        court_id=getattr(obj, "court_id", None),
        start_at=getattr(obj, "start_at", None),
        end_at=getattr(obj, "end_at", None),
    )


def _waiting_waitlist_qs_for_slot(*, coach, court, lesson_type, start_at, end_at):
    return (
        LessonWaitlist.objects.select_related("user", "coach", "substitute_coach", "court", "availability", "fixed_lesson")
        .filter(
            coach=coach,
            court=court,
            lesson_type=lesson_type,
            start_at=start_at,
            end_at=end_at,
            status=LessonWaitlist.STATUS_WAITING,
        )
        .order_by("created_at", "id")
    )


def _active_reservation_count_for_slot(*, coach, court, lesson_type, start_at, end_at):
    return Reservation.objects.filter(
        coach=coach,
        court=court,
        lesson_type=lesson_type,
        start_at=start_at,
        end_at=end_at,
        status=Reservation.STATUS_ACTIVE,
    ).count()


def _capacity_for_reservation_slot(reservation):
    availability = getattr(reservation, "availability", None) or reservation.matching_availability()
    if availability:
        try:
            return max(int(availability.effective_capacity()), int(availability.capacity or 0), 1)
        except Exception:
            return max(int(getattr(availability, "capacity", 1) or 1), 1)

    fixed_lesson = getattr(reservation, "fixed_lesson", None)
    if fixed_lesson:
        try:
            return max(int(fixed_lesson.effective_capacity()), int(fixed_lesson.capacity or 0), 1)
        except Exception:
            return max(int(getattr(fixed_lesson, "capacity", 1) or 1), 1)

    return 1


def _notify_first_waitlist_user_if_slot_open(reservation):
    if not reservation:
        return False

    active_count = _active_reservation_count_for_slot(
        coach=reservation.coach,
        court=reservation.court,
        lesson_type=reservation.lesson_type,
        start_at=reservation.start_at,
        end_at=reservation.end_at,
    )
    capacity = _capacity_for_reservation_slot(reservation)

    if active_count >= capacity:
        return False

    waitlist = _waiting_waitlist_qs_for_slot(
        coach=reservation.coach,
        court=reservation.court,
        lesson_type=reservation.lesson_type,
        start_at=reservation.start_at,
        end_at=reservation.end_at,
    ).first()

    if not waitlist:
        return False

    _send_line_notification_safely(waitlist.user, _build_waitlist_opening_for_member_message(waitlist))
    return True



def _capacity_for_waitlist_slot(waitlist):
    availability = getattr(waitlist, "availability", None)
    if availability:
        try:
            return max(int(availability.effective_capacity()), int(availability.capacity or 0), 1)
        except Exception:
            return max(int(getattr(availability, "capacity", 1) or 1), 1)

    fixed_lesson = getattr(waitlist, "fixed_lesson", None)
    if fixed_lesson:
        try:
            return max(int(fixed_lesson.effective_capacity()), int(fixed_lesson.capacity or 0), 1)
        except Exception:
            return max(int(getattr(fixed_lesson, "capacity", 1) or 1), 1)

    return 1


def _user_can_manage_waitlist(user, waitlist):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    if waitlist.user_id == user.pk:
        return True
    if getattr(user, "role", None) in ("coach", "contractor_coach"):
        return (
            waitlist.coach_id == user.pk
            or getattr(waitlist, "substitute_coach_id", None) == user.pk
        )
    return False


def _coach_can_manage_waitlist(user, waitlist):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    if getattr(user, "role", None) not in ("coach", "contractor_coach"):
        return False
    return (
        waitlist.coach_id == user.pk
        or getattr(waitlist, "substitute_coach_id", None) == user.pk
    )


def _build_waitlist_promoted_for_member_message(reservation):
    label = _lesson_waitlist_lesson_label(reservation)
    return (
        "キャンセル待ちから予約に繰り上がりました。\n\n"
        f"日時：{label['date']} {label['time']}\n"
        f"レッスン：{label['lesson']}\n"
        f"レベル：{label['level']}\n"
        f"コーチ：{label['coach']}\n"
        f"コート：{label['court']}\n\n"
        "予約内容は予約確認画面からご確認ください。"
    )


def _build_waitlist_promoted_for_coach_message(reservation):
    label = _lesson_waitlist_lesson_label(reservation)
    return (
        "キャンセル待ちから予約へ繰り上げました。\n\n"
        f"会員：{_display_name(reservation.user)}\n"
        f"日時：{label['date']} {label['time']}\n"
        f"レッスン：{label['lesson']}\n"
        f"レベル：{label['level']}\n"
        f"コート：{label['court']}"
    )


def _availability_can_manage(user, availability):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if _is_staff_like(user):
        return True
    if _is_coach_user(user) and getattr(availability, "coach_id", None) == getattr(user, "pk", None):
        return True
    return False


def _active_reservations_for_availability(availability):
    return list(
        Reservation.objects.select_related("user", "coach", "substitute_coach", "court")
        .filter(
            coach=availability.coach,
            court=availability.court,
            lesson_type=availability.lesson_type,
            start_at=availability.start_at,
            end_at=availability.end_at,
            status=Reservation.STATUS_ACTIVE,
        )
        .order_by("start_at", "id")
    )



def _lesson_calendar_duration_hours(fixed_lesson):
    if fixed_lesson.lesson_type == FixedLesson.LESSON_GENERAL:
        return 2
    return 1


def _lesson_calendar_title(fixed_lesson):
    if getattr(fixed_lesson, "title", ""):
        return fixed_lesson.title
    return fixed_lesson.get_lesson_type_display()


def _fixed_lesson_coach_names(fixed_lesson):
    try:
        return fixed_lesson.coach_display_names()
    except Exception:
        return _display_name(getattr(fixed_lesson, "coach", None))


def _fixed_lesson_includes_coach(fixed_lesson, coach):
    if not fixed_lesson or not coach:
        return False
    try:
        return fixed_lesson.includes_coach(coach)
    except Exception:
        return getattr(fixed_lesson, "coach_id", None) == getattr(coach, "pk", None)



@require_http_methods(["GET", "POST"])
def lesson_calendar_view(request):
    _sync_fixed_lessons()

    today = timezone.localdate()

    def _parse_target_month(raw_year, raw_month):
        try:
            year_value = int(raw_year or today.year)
        except Exception:
            year_value = today.year

        try:
            month_value = int(raw_month or today.month)
        except Exception:
            month_value = today.month

        if month_value < 1 or month_value > 12:
            month_value = today.month

        if year_value < today.year - 1 or year_value > today.year + 2:
            year_value = today.year

        return year_value, month_value

    def _local_dt(value):
        if timezone.is_aware(value):
            return timezone.localtime(value)
        return value

    def _first_active_court():
        return Court.objects.filter(is_active=True).order_by("id").first()

    def _fixed_lesson_datetimes_safely(fixed_lesson, target_date):
        try:
            return fixed_lesson._build_datetimes_for_date(target_date)
        except Exception:
            try:
                start_hour = int(getattr(fixed_lesson, "start_hour", 0) or 0)
                if start_hour < 0 or start_hour > 23:
                    return None, None
                start_dt = datetime.combine(target_date, datetime.min.time()).replace(hour=start_hour, minute=0)
                if timezone.is_naive(start_dt):
                    start_dt = timezone.make_aware(start_dt)
                duration_hours = _lesson_calendar_duration_hours(fixed_lesson)
                return start_dt, start_dt + timedelta(hours=duration_hours)
            except Exception:
                return None, None

    def _capacity_for_fixed_lesson(fixed_lesson):
        try:
            value = int(fixed_lesson.effective_capacity())
        except Exception:
            value = int(getattr(fixed_lesson, "capacity", 0) or 0)
        return max(value, int(getattr(fixed_lesson, "capacity", 0) or 0), 1)

    def _capacity_for_availability(availability):
        try:
            value = int(availability.effective_capacity())
        except Exception:
            value = int(getattr(availability, "capacity", 0) or 0)
        return max(value, int(getattr(availability, "capacity", 0) or 0), 1)

    def _find_matching_availability_for_fixed(fixed_lesson, start_at, end_at):
        primary_coach = fixed_lesson.primary_coach() if hasattr(fixed_lesson, "primary_coach") else fixed_lesson.coach
        qs = CoachAvailability.objects.select_related("coach", "substitute_coach", "court").filter(
            coach=primary_coach,
            lesson_type=fixed_lesson.lesson_type,
            start_at=start_at,
            end_at=end_at,
        )
        if getattr(fixed_lesson, "court_id", None):
            qs = qs.filter(court=fixed_lesson.court)
        return qs.order_by("id").first()

    def _get_or_create_availability_from_fixed_lesson(fixed_lesson, target_date):
        start_at, end_at = _fixed_lesson_datetimes_safely(fixed_lesson, target_date)
        if not start_at or not end_at:
            raise ValidationError("固定レッスンの日付・時間を作成できませんでした。")

        repeat_start = getattr(fixed_lesson, "start_date", None)
        if repeat_start and target_date < repeat_start:
            raise ValidationError("この固定レッスンは、指定日の時点ではまだ開始前です。")

        primary_coach = fixed_lesson.primary_coach() if hasattr(fixed_lesson, "primary_coach") else fixed_lesson.coach
        court = fixed_lesson.court or _first_active_court()
        if not court:
            raise ValidationError("予約に利用できるコートが登録されていません。")

        existing = CoachAvailability.objects.filter(
            coach=primary_coach,
            lesson_type=fixed_lesson.lesson_type,
            start_at=start_at,
            end_at=end_at,
        ).order_by("id").first()
        if existing:
            return existing

        availability = CoachAvailability(
            coach=primary_coach,
            court=court,
            lesson_type=fixed_lesson.lesson_type,
            target_level=fixed_lesson.target_level,
            target_level_2=getattr(fixed_lesson, "target_level_2", "") or "",
            start_at=start_at,
            end_at=end_at,
            capacity=_capacity_for_fixed_lesson(fixed_lesson),
            coach_count=max(int(getattr(fixed_lesson, "coach_count", 1) or 1), 1),
            court_count=max(int(getattr(fixed_lesson, "court_count", 1) or 1), 1),
            status=CoachAvailability.STATUS_OPEN,
            note=f"固定レッスン: {fixed_lesson.title or fixed_lesson.get_weekday_display()}",
        )
        availability.save()
        return availability

    if request.method == "POST":
        target_year, target_month = _parse_target_month(request.POST.get("year"), request.POST.get("month"))
        redirect_url = f"{reverse('club:lesson_calendar')}?{urlencode({'year': target_year, 'month': target_month})}"
        action = (request.POST.get("action") or "reserve").strip()

        if not request.user.is_authenticated:
            messages.info(request, "予約するにはログインしてください。")
            return redirect("club:line_login_start")

        profile_redirect = _require_profile_completed_for_booking(request)
        if profile_redirect:
            return profile_redirect

        survey_redirect = _require_schedule_survey(request)
        if survey_redirect:
            return survey_redirect

        if not _can_user_take_lessons(request.user):
            messages.error(request, "通常レッスンの予約・キャンセル待ちは会員または業務委託コーチアカウントで行ってください。")
            return redirect(redirect_url)

        availability_id = (request.POST.get("availability_id") or "").strip()
        fixed_lesson_id = (request.POST.get("fixed_lesson_id") or "").strip()
        lesson_date_text = (request.POST.get("lesson_date") or "").strip()

        try:
            fixed_lesson = None
            # 固定レッスン由来の枠では、固定参加メンバー数を満員判定に含める必要があるため、
            # availability_id が同時に渡っていても fixed_lesson_id + lesson_date を優先する。
            if fixed_lesson_id and lesson_date_text:
                fixed_lesson = get_object_or_404(
                    FixedLesson.objects.select_related("coach", "coach_2", "coach_3", "court"),
                    pk=fixed_lesson_id,
                    is_active=True,
                )
                try:
                    target_date = date.fromisoformat(lesson_date_text)
                except Exception:
                    raise ValidationError("予約対象日が正しくありません。")
                repeat_start = getattr(fixed_lesson, "start_date", None)
                if repeat_start and target_date < repeat_start:
                    raise ValidationError("この固定レッスンはまだ開始前です。")
                availability = _get_or_create_availability_from_fixed_lesson(fixed_lesson, target_date)
                if availability.status != CoachAvailability.STATUS_OPEN:
                    raise ValidationError("このレッスンはまだ受付準備中です。")
                if availability.start_at < timezone.now():
                    raise ValidationError("このレッスンは受付終了です。")
                if availability.lesson_type not in (Reservation.LESSON_GENERAL, Reservation.LESSON_EVENT):
                    raise ValidationError("このレッスンは個別相談フォームから申請してください。")
            elif availability_id:
                availability = get_object_or_404(
                    CoachAvailability.objects.select_related("coach", "substitute_coach", "court"),
                    pk=availability_id,
                    lesson_type__in=[Reservation.LESSON_GENERAL, Reservation.LESSON_EVENT],
                )
                if availability.status != CoachAvailability.STATUS_OPEN:
                    raise ValidationError("このレッスンはまだ受付準備中です。")
                if availability.start_at < timezone.now():
                    raise ValidationError("このレッスンは受付終了です。")
            else:
                raise ValidationError("対象のレッスンが見つかりません。")

            participant = resolve_reservation_participant(
                request.user,
                request.POST.get("participant_key") or "self",
            )
            validate_participant_can_book_lesson(
                participant,
                availability.target_level,
                getattr(availability, "target_level_2", "") or "",
            )

            active_count = Reservation.objects.filter(
                coach=availability.coach,
                court=availability.court,
                lesson_type=availability.lesson_type,
                start_at=availability.start_at,
                end_at=availability.end_at,
                status=Reservation.STATUS_ACTIVE,
            ).count()
            # 固定レッスンの固定参加メンバーは、予約レコードが未生成・未同期の場合でも
            # レッスン枠の参加人数として扱う。これにより、固定参加メンバーだけで満員の場合も
            # 会員側からキャンセル待ち登録できる。
            if fixed_lesson is not None:
                try:
                    fixed_member_count = fixed_lesson.members.count()
                except Exception:
                    fixed_member_count = 0
                active_count = max(int(active_count or 0), int(fixed_member_count or 0))
            capacity = _capacity_for_availability(availability)

            existing_waitlist = LessonWaitlist.objects.filter(
                user=request.user,
                coach=availability.coach,
                court=availability.court,
                lesson_type=availability.lesson_type,
                start_at=availability.start_at,
                end_at=availability.end_at,
                status=LessonWaitlist.STATUS_WAITING,
            ).first()

            if action == "join_waitlist":
                if active_count < capacity:
                    messages.info(request, "このレッスンはまだ空きがあります。予約画面から予約してください。")
                    return redirect(redirect_url)
                if existing_waitlist:
                    messages.info(request, "このレッスンはすでにキャンセル待ち登録済みです。")
                    return redirect(redirect_url)

                waitlist = LessonWaitlist(
                    user=request.user,
                    coach=availability.coach,
                    substitute_coach=availability.substitute_coach,
                    court=availability.court,
                    availability=availability,
                    fixed_lesson=fixed_lesson,
                    lesson_type=availability.lesson_type,
                    target_level=availability.target_level,
                    target_level_2=getattr(availability, "target_level_2", "") or "",
                    start_at=availability.start_at,
                    end_at=availability.end_at,
                    status=LessonWaitlist.STATUS_WAITING,
                    note="レッスンカレンダーから登録",
                )
                waitlist.save()
                _send_email_notification_safely(
                    waitlist.user,
                    "【Play Design Tennis】キャンセル待ち登録完了",
                    build_waitlist_registered_for_member_email_message(waitlist),
                )
                messages.success(request, "キャンセル待ちに登録しました。空きが出た場合はLINEでご案内します。")
                return redirect(redirect_url)

            if action == "cancel_waitlist":
                if not existing_waitlist:
                    messages.info(request, "キャンセル待ち登録は見つかりませんでした。")
                    return redirect(redirect_url)
                existing_waitlist.cancel(reason="会員がレッスンカレンダーからキャンセル")
                messages.success(request, "キャンセル待ちを取り消しました。")
                return redirect(redirect_url)

            if active_count >= capacity:
                messages.error(request, "このレッスンは満員です。キャンセル待ちをご利用ください。")
                return redirect(redirect_url)

            with transaction.atomic():
                reservation = Reservation(
                    user=request.user,
                    coach=availability.coach,
                    substitute_coach=availability.substitute_coach,
                    court=availability.court,
                    availability=availability,
                    fixed_lesson=fixed_lesson,
                    lesson_type=availability.lesson_type,
                    target_level=availability.target_level,
                    target_level_2=getattr(availability, "target_level_2", "") or "",
                    start_at=availability.start_at,
                    end_at=availability.end_at,
                    status=Reservation.STATUS_ACTIVE,
                    custom_ticket_price=availability.custom_ticket_price,
                    custom_duration_hours=availability.custom_duration_hours,
                )
                reservation.full_clean()
                reservation.save()
                save_reservation_participant_snapshot(reservation, participant)
                if reservation.tickets_used > 0:
                    reservation.consume_tickets(
                        reason=TicketLedger.REASON_RESERVATION_USE,
                        created_by=request.user,
                        note=f"通常レッスン予約: {availability.start_at:%Y-%m-%d %H:%M}",
                    )
                if existing_waitlist:
                    existing_waitlist.mark_converted()

            member_message = build_reservation_created_message(reservation)
            _send_email_notification_safely(
                reservation.user,
                "【Play Design Tennis】予約完了通知",
                member_message,
            )

            messages.success(request, "レッスン予約が完了しました。")
            return redirect("club:reservation_detail", pk=reservation.pk)

        except ValidationError as e:
            if hasattr(e, "messages"):
                for message_text in e.messages:
                    messages.error(request, message_text)
            else:
                messages.error(request, str(e))
        except Exception as e:
            messages.error(request, f"処理中にエラーが発生しました: {e}")

        return redirect(redirect_url)

    target_year, target_month = _parse_target_month(request.GET.get("year"), request.GET.get("month"))
    month_start, next_month = _month_start_end(target_year, target_month)

    prev_year = target_year
    prev_month = target_month - 1
    if prev_month == 0:
        prev_month = 12
        prev_year -= 1

    next_year = target_year
    next_month_number = target_month + 1
    if next_month_number == 13:
        next_month_number = 1
        next_year += 1

    reservation_qs = (
        Reservation.objects.filter(
            status__in=[Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING],
            start_at__date__gte=month_start,
            start_at__date__lt=next_month,
        )
        .select_related("user", "coach", "substitute_coach", "court", "availability", "fixed_lesson")
        .order_by("start_at", "id")
    )
    reservation_list = list(reservation_qs)

    active_slot_counts = {}
    pending_slot_counts = {}
    fixed_lesson_active_counts = {}
    fixed_lesson_pending_counts = {}
    user_slot_status_map = {}
    user_fixed_lesson_status_map = {}

    for reservation in reservation_list:
        slot_key = _slot_key(
            lesson_type=reservation.lesson_type,
            coach_id=reservation.coach_id,
            court_id=reservation.court_id,
            start_at=reservation.start_at,
            end_at=reservation.end_at,
        )

        fixed_lesson_key = None
        if getattr(reservation, "fixed_lesson_id", None):
            try:
                reservation_start_local = _local_dt(reservation.start_at)
                fixed_lesson_key = (str(reservation.fixed_lesson_id), reservation_start_local.date().isoformat())
            except Exception:
                fixed_lesson_key = None

        if reservation.status == Reservation.STATUS_ACTIVE:
            active_slot_counts.setdefault(slot_key, 0)
            active_slot_counts[slot_key] += 1
            if fixed_lesson_key:
                fixed_lesson_active_counts.setdefault(fixed_lesson_key, 0)
                fixed_lesson_active_counts[fixed_lesson_key] += 1
        elif reservation.status == Reservation.STATUS_PENDING:
            pending_slot_counts.setdefault(slot_key, 0)
            pending_slot_counts[slot_key] += 1
            if fixed_lesson_key:
                fixed_lesson_pending_counts.setdefault(fixed_lesson_key, 0)
                fixed_lesson_pending_counts[fixed_lesson_key] += 1

        if request.user.is_authenticated and reservation.user_id == request.user.pk:
            user_slot_status_map[slot_key] = reservation.status
            if fixed_lesson_key:
                current_fixed_status = user_fixed_lesson_status_map.get(fixed_lesson_key, "")
                if reservation.status == Reservation.STATUS_ACTIVE or not current_fixed_status:
                    user_fixed_lesson_status_map[fixed_lesson_key] = reservation.status

    waitlist_qs = (
        LessonWaitlist.objects.filter(
            status=LessonWaitlist.STATUS_WAITING,
            start_at__date__gte=month_start,
            start_at__date__lt=next_month,
        )
        .select_related("user", "coach", "substitute_coach", "court", "availability", "fixed_lesson")
        .order_by("start_at", "created_at", "id")
    )
    waitlist_counts = {}
    fixed_lesson_waitlist_counts = {}
    user_waitlist_map = {}
    user_fixed_lesson_waitlist_map = {}
    for waitlist in waitlist_qs:
        slot_key = _slot_key(
            lesson_type=waitlist.lesson_type,
            coach_id=waitlist.coach_id,
            court_id=waitlist.court_id,
            start_at=waitlist.start_at,
            end_at=waitlist.end_at,
        )
        waitlist_counts.setdefault(slot_key, 0)
        waitlist_counts[slot_key] += 1

        fixed_waitlist_key = None
        if getattr(waitlist, "fixed_lesson_id", None):
            try:
                waitlist_start_local = _local_dt(waitlist.start_at)
                fixed_waitlist_key = (str(waitlist.fixed_lesson_id), waitlist_start_local.date().isoformat())
            except Exception:
                fixed_waitlist_key = None

        if fixed_waitlist_key:
            fixed_lesson_waitlist_counts.setdefault(fixed_waitlist_key, 0)
            fixed_lesson_waitlist_counts[fixed_waitlist_key] += 1

        if request.user.is_authenticated and waitlist.user_id == request.user.pk:
            user_waitlist_map[slot_key] = waitlist.pk
            if fixed_waitlist_key:
                user_fixed_lesson_waitlist_map[fixed_waitlist_key] = waitlist.pk

    weekday_short = ["月", "火", "水", "木", "金", "土", "日"]

    def _coach_color_from_names(names, coach_id=None):
        names = names or ""
        if "清水" in names and "飯塚" not in names:
            return "coach-green"
        if "井上" in names and "飯塚" not in names and "清水" not in names:
            return "coach-purple"
        if "清水" in names:
            return "coach-green"
        if "井上" in names:
            return "coach-purple"
        if "飯塚" in names:
            return "coach-blue"
        if coach_id:
            color_classes = ["coach-blue", "coach-green", "coach-purple", "coach-orange"]
            return color_classes[int(coach_id) % len(color_classes)]
        return "coach-blue"

    def _coach_combo_class_from_names(names):
        raw_names = str(names or "").strip()
        if not raw_names:
            return ""

        normalized = (
            raw_names
            .replace("／", "/")
            .replace("、", "/")
            .replace(",", "/")
            .replace("・", "/")
        )

        color_order = []
        for part in normalized.split("/"):
            clean_name = part.strip()
            if not clean_name:
                continue
            if "飯塚" in clean_name:
                color = "blue"
            elif "清水" in clean_name:
                color = "green"
            elif "井上" in clean_name:
                color = "purple"
            else:
                color = "orange"

            if color not in color_order:
                color_order.append(color)

        if len(color_order) >= 2:
            return f"coach-split-{color_order[0]}-{color_order[1]}"
        return ""

    def _coach_name_color_class(name):
        name = str(name or "")
        if "飯塚" in name:
            return "coach-name-blue"
        if "清水" in name:
            return "coach-name-green"
        if "井上" in name:
            return "coach-name-purple"
        return "coach-name-orange"

    def _coach_name_parts_from_names(names):
        raw_names = str(names or "").strip()
        if not raw_names:
            return []

        normalized = (
            raw_names
            .replace("／", "/")
            .replace("、", "/")
            .replace(",", "/")
            .replace("・", "/")
        )
        parts = []
        for name in normalized.split("/"):
            clean_name = name.strip()
            if not clean_name:
                continue
            parts.append(
                {
                    "name": clean_name,
                    "color_class": _coach_name_color_class(clean_name),
                }
            )
        if parts:
            return parts

        return [
            {
                "name": raw_names,
                "color_class": _coach_name_color_class(raw_names),
            }
        ]

    def _coach_color_class(availability):
        names = " ".join(
            [
                _display_name(getattr(availability, "coach", None)),
                _display_name(getattr(availability, "substitute_coach", None)) if getattr(availability, "substitute_coach_id", None) else "",
            ]
        )
        return _coach_color_from_names(names, getattr(availability, "coach_id", None))

    def _build_display_item(
        *,
        item_id,
        source_kind,
        title,
        lesson_type,
        lesson_type_label,
        target_level,
        target_level_label,
        target_level_2,
        start_at,
        end_at,
        coach,
        assigned_coach_name,
        substitute_coach=None,
        court=None,
        capacity=1,
        member_count=0,
        pending_count=0,
        waitlist_count=0,
        status=CoachAvailability.STATUS_OPEN,
        color_class="coach-blue",
        color_combo_class="",
        availability_id="",
        fixed_lesson_id="",
        lesson_date="",
        allow_fixed_booking=False,
        user_slot_status_override="",
        user_waitlist_id_override="",
    ):
        start_local = _local_dt(start_at)
        end_local = _local_dt(end_at)
        target_date = start_local.date()
        weekday_label = weekday_short[target_date.weekday()]
        remaining_count = max(int(capacity or 0) - int(member_count or 0), 0)

        can_book = False
        can_join_waitlist = False
        can_cancel_waitlist = False
        disabled_reason = ""

        slot_key = _slot_key(
            lesson_type=lesson_type,
            coach_id=getattr(coach, "pk", None),
            court_id=getattr(court, "pk", None),
            start_at=start_at,
            end_at=end_at,
        )
        user_slot_status = user_slot_status_override or user_slot_status_map.get(slot_key, "")
        user_waitlist_id = user_waitlist_id_override or user_waitlist_map.get(slot_key, "")

        if start_at < timezone.now():
            disabled_reason = "受付終了"
        elif source_kind != "availability" and not allow_fixed_booking:
            disabled_reason = "受付準備中"
        elif status != CoachAvailability.STATUS_OPEN and not allow_fixed_booking:
            disabled_reason = "受付準備中"
        elif lesson_type not in (Reservation.LESSON_GENERAL, Reservation.LESSON_EVENT):
            disabled_reason = "個別相談から申請"
        elif not request.user.is_authenticated:
            disabled_reason = "ログインすると予約できます。"
        elif not _can_user_take_lessons(request.user):
            disabled_reason = "会員または業務委託コーチアカウントで予約できます。"
        elif user_slot_status == Reservation.STATUS_ACTIVE:
            disabled_reason = "予約済みです。"
        elif user_slot_status == Reservation.STATUS_PENDING:
            disabled_reason = "承認待ちの申請があります。"
        elif not _slot_level_allowed(request.user, target_level, target_level_2):
            disabled_reason = "ご自身のレベルでは予約できません。"
        elif int(member_count or 0) >= int(capacity or 0):
            if user_waitlist_id:
                disabled_reason = "キャンセル待ち中です。"
                can_cancel_waitlist = True
            else:
                disabled_reason = "満員です。"
                can_join_waitlist = True
        elif source_kind == "availability" and availability_id:
            can_book = True
        elif source_kind == "fixed_lesson" and fixed_lesson_id and lesson_date and allow_fixed_booking:
            can_book = True
        else:
            disabled_reason = "受付準備中"

        reserve_params = {
            "year": target_year,
            "month": target_month,
        }
        if source_kind == "fixed_lesson" and fixed_lesson_id and lesson_date:
            reserve_params["fixed_lesson_id"] = fixed_lesson_id
            reserve_params["lesson_date"] = lesson_date
        elif availability_id:
            reserve_params["availability_id"] = availability_id
        elif fixed_lesson_id and lesson_date:
            reserve_params["fixed_lesson_id"] = fixed_lesson_id
            reserve_params["lesson_date"] = lesson_date

        reserve_url = f"{reverse('club:lesson_reservation_confirm')}?{urlencode(reserve_params)}"
        login_url = f"{reverse('club:line_login_start')}?{urlencode({'next': reserve_url})}"

        return {
            "id": item_id,
            "availability_id": availability_id,
            "fixed_lesson_id": fixed_lesson_id,
            "lesson_date": lesson_date,
            "reserve_url": reserve_url,
            "login_url": login_url,
            "source_kind": source_kind,
            "title": title,
            "date_label": start_local.strftime("%m/%d"),
            "date_label_jp": f"{target_date.month}/{target_date.day}（{weekday_label}）",
            "day_number": target_date.day,
            "time_label": f"{start_local:%H:%M}〜{end_local:%H:%M}",
            "sort_key": f"{start_local:%Y%m%d%H%M}-{item_id}",
            "coach_name": assigned_coach_name,
            "coach_name_parts": _coach_name_parts_from_names(assigned_coach_name),
            "normal_coach_name": _display_name(coach),
            "substitute_coach_name": _display_name(substitute_coach) if substitute_coach else "",
            "has_substitute": bool(substitute_coach),
            "court_name": str(court) if court else "未定",
            "lesson_type_label": lesson_type_label,
            "target_level_label": target_level_label,
            "target_level_2": target_level_2,
            "capacity": capacity,
            "member_count": int(member_count or 0),
            "pending_count": int(pending_count or 0),
            "remaining_count": remaining_count,
            "is_past": target_date < today,
            "can_book": can_book,
            "can_join_waitlist": can_join_waitlist,
            "can_cancel_waitlist": can_cancel_waitlist,
            "user_waitlist_id": user_waitlist_id,
            "waitlist_count": int(waitlist_count or 0),
            "is_reserved_by_user": user_slot_status == Reservation.STATUS_ACTIVE,
            "is_waitlisted_by_user": bool(user_waitlist_id),
            "disabled_reason": disabled_reason,
            "color_class": color_class,
            "color_combo_class": color_combo_class,
        }

    day_event_map = {}
    schedule_rows = []
    represented_availability_ids = set()

    fixed_lesson_list = list(
        FixedLesson.objects.filter(is_active=True)
        .select_related("coach", "coach_2", "coach_3", "court")
        .prefetch_related("members")
        .order_by("weekday", "start_hour", "id")
    )

    for fixed_lesson in fixed_lesson_list:
        if hasattr(fixed_lesson, "scheduled_occurrence_dates"):
            occurrence_dates = fixed_lesson.scheduled_occurrence_dates()
        else:
            repeat_start = getattr(fixed_lesson, "start_date", None) or month_start
            first_offset = (int(fixed_lesson.weekday) - repeat_start.weekday()) % 7
            first_date = repeat_start + timedelta(days=first_offset)
            try:
                occurrence_count = max(int(getattr(fixed_lesson, "weeks_ahead", 1) or 1), 1)
            except Exception:
                occurrence_count = 1
            occurrence_dates = [
                first_date + timedelta(days=7 * index)
                for index in range(occurrence_count)
            ]

        for cursor_date in occurrence_dates:
            if cursor_date < month_start or cursor_date >= next_month:
                continue

            start_at, end_at = _fixed_lesson_datetimes_safely(fixed_lesson, cursor_date)
            if not start_at or not end_at:
                continue

            primary_coach = fixed_lesson.primary_coach() if hasattr(fixed_lesson, "primary_coach") else fixed_lesson.coach
            matching_availability = _find_matching_availability_for_fixed(fixed_lesson, start_at, end_at)
            if matching_availability:
                represented_availability_ids.add(matching_availability.pk)
                court = matching_availability.court
                capacity = _capacity_for_availability(matching_availability)
                status = matching_availability.status
                availability_id = str(matching_availability.pk)
                substitute_coach = matching_availability.substitute_coach
                slot_coach = matching_availability.coach
                slot_key = _slot_key(
                    lesson_type=matching_availability.lesson_type,
                    coach_id=matching_availability.coach_id,
                    court_id=matching_availability.court_id,
                    start_at=matching_availability.start_at,
                    end_at=matching_availability.end_at,
                )
            else:
                court = fixed_lesson.court or _first_active_court()
                capacity = _capacity_for_fixed_lesson(fixed_lesson)
                status = CoachAvailability.STATUS_OPEN
                availability_id = ""
                substitute_coach = None
                slot_coach = primary_coach
                slot_key = _slot_key(
                    lesson_type=fixed_lesson.lesson_type,
                    coach_id=getattr(primary_coach, "pk", None),
                    court_id=getattr(court, "pk", None),
                    start_at=start_at,
                    end_at=end_at,
                )

            fixed_member_list = list(fixed_lesson.members.all())
            fixed_member_count = len(fixed_member_list)
            fixed_key = (str(fixed_lesson.pk), cursor_date.isoformat())
            member_count = max(
                int(active_slot_counts.get(slot_key, 0)),
                int(fixed_lesson_active_counts.get(fixed_key, 0)),
                fixed_member_count,
            )
            pending_count = max(
                int(pending_slot_counts.get(slot_key, 0)),
                int(fixed_lesson_pending_counts.get(fixed_key, 0)),
            )
            fixed_user_status = user_fixed_lesson_status_map.get(fixed_key, "")
            fixed_user_waitlist_id = user_fixed_lesson_waitlist_map.get(fixed_key, "")

            if (
                not fixed_user_status
                and request.user.is_authenticated
                and request.user.pk in {member.pk for member in fixed_member_list}
            ):
                fixed_user_status = Reservation.STATUS_ACTIVE

            coach_names = _fixed_lesson_coach_names(fixed_lesson)

            item = _build_display_item(
                item_id=f"fixed-{fixed_lesson.pk}-{cursor_date:%Y%m%d}",
                availability_id=availability_id,
                fixed_lesson_id=str(fixed_lesson.pk),
                lesson_date=cursor_date.isoformat(),
                source_kind="fixed_lesson",
                title=_lesson_calendar_title(fixed_lesson),
                lesson_type=fixed_lesson.lesson_type,
                lesson_type_label=fixed_lesson.get_lesson_type_display(),
                target_level=fixed_lesson.target_level,
                target_level_label=_lesson_level_label(fixed_lesson) or fixed_lesson.get_target_level_display(),
                target_level_2=getattr(fixed_lesson, "target_level_2", "") or "",
                start_at=start_at,
                end_at=end_at,
                coach=slot_coach,
                assigned_coach_name=coach_names,
                substitute_coach=substitute_coach,
                court=court,
                capacity=capacity,
                member_count=member_count,
                pending_count=pending_count,
                waitlist_count=max(
                    int(waitlist_counts.get(slot_key, 0)),
                    int(fixed_lesson_waitlist_counts.get(fixed_key, 0)),
                ),
                status=status,
                color_class=_coach_color_from_names(coach_names, getattr(primary_coach, "pk", None)),
                color_combo_class=_coach_combo_class_from_names(coach_names),
                allow_fixed_booking=bool(court),
                user_slot_status_override=fixed_user_status,
                user_waitlist_id_override=fixed_user_waitlist_id,
            )

            day_event_map.setdefault(cursor_date, [])
            day_event_map[cursor_date].append(item)
            schedule_rows.append(item)

    availability_qs = (
        CoachAvailability.objects.filter(
            start_at__date__gte=month_start,
            start_at__date__lt=next_month,
        )
        .select_related("coach", "substitute_coach", "court")
        .order_by("start_at", "coach__username", "court__name", "id")
    )
    availability_list = list(availability_qs)

    for availability in availability_list:
        if availability.pk in represented_availability_ids:
            continue

        # レッスンカレンダーは FixedLesson を正とする。
        # 過去の固定レッスン同期で残った一般レッスンの CoachAvailability をそのまま出すと、
        # 管理画面の FixedLesson に無い枠まで表示されてしまうため、一般レッスンの単独枠は表示しない。
        # イベント等を CoachAvailability 単独で登録した場合だけ、追加枠として表示する。
        if availability.lesson_type == Reservation.LESSON_GENERAL:
            continue

        start_local = _local_dt(availability.start_at)
        target_date = start_local.date()

        slot_key = _slot_key(
            lesson_type=availability.lesson_type,
            coach_id=availability.coach_id,
            court_id=availability.court_id,
            start_at=availability.start_at,
            end_at=availability.end_at,
        )

        member_count = int(active_slot_counts.get(slot_key, 0))
        pending_count = int(pending_slot_counts.get(slot_key, 0))
        capacity = _capacity_for_availability(availability)
        assigned_coach = availability.assigned_coach() if hasattr(availability, "assigned_coach") else (availability.substitute_coach or availability.coach)

        item = _build_display_item(
            item_id=str(availability.pk),
            availability_id=str(availability.pk),
            source_kind="availability",
            title="通常レッスン" if availability.lesson_type == Reservation.LESSON_GENERAL else availability.get_lesson_type_display(),
            lesson_type=availability.lesson_type,
            lesson_type_label=availability.get_lesson_type_display(),
            target_level=availability.target_level,
            target_level_label=_lesson_level_label(availability) or availability.get_target_level_display(),
            target_level_2=getattr(availability, "target_level_2", "") or "",
            start_at=availability.start_at,
            end_at=availability.end_at,
            coach=availability.coach,
            assigned_coach_name=_display_name(assigned_coach),
            substitute_coach=availability.substitute_coach,
            court=availability.court,
            capacity=capacity,
            member_count=member_count,
            pending_count=pending_count,
            waitlist_count=int(waitlist_counts.get(slot_key, 0)),
            status=availability.status,
            color_class=_coach_color_class(availability),
            color_combo_class=_coach_combo_class_from_names(_display_name(assigned_coach)),
        )

        day_event_map.setdefault(target_date, [])
        day_event_map[target_date].append(item)
        schedule_rows.append(item)

    for target_date, items in day_event_map.items():
        day_event_map[target_date] = sorted(items, key=lambda row: row.get("sort_key", ""))

    schedule_rows = sorted(schedule_rows, key=lambda row: row.get("sort_key", ""))

    calendar_start = month_start - timedelta(days=month_start.weekday())
    calendar_weeks = []
    cursor = calendar_start
    for _week_index in range(6):
        week = []
        for _day_index in range(7):
            week.append(
                {
                    "date": cursor,
                    "day_number": cursor.day,
                    "is_current_month": cursor.month == target_month,
                    "is_today": cursor == today,
                    "is_past": cursor < today,
                    "is_saturday": cursor.weekday() == 5,
                    "is_sunday": cursor.weekday() == 6,
                    "items": day_event_map.get(cursor, []),
                }
            )
            cursor += timedelta(days=1)
        calendar_weeks.append(week)

    return render(
        request,
        "lesson_calendar.html",
        {
            "target_year": target_year,
            "target_month": target_month,
            "month_title": f"{target_year}年{target_month}月",
            "prev_year": prev_year,
            "prev_month": prev_month,
            "next_year": next_year,
            "next_month": next_month_number,
            "weekday_headers": ["月", "火", "水", "木", "金", "土", "日"],
            "calendar_weeks": calendar_weeks,
            "schedule_rows": schedule_rows,
            "display_range_days": 45,
        },
    )


@login_required
@require_GET
def lesson_reservation_confirm(request):
    _sync_fixed_lessons()

    today = timezone.localdate()

    def _parse_target_month(raw_year, raw_month):
        try:
            year_value = int(raw_year or today.year)
        except Exception:
            year_value = today.year

        try:
            month_value = int(raw_month or today.month)
        except Exception:
            month_value = today.month

        if month_value < 1 or month_value > 12:
            month_value = today.month

        if year_value < today.year - 1 or year_value > today.year + 2:
            year_value = today.year

        return year_value, month_value

    def _local(value):
        if timezone.is_aware(value):
            return timezone.localtime(value)
        return value

    def _first_active_court():
        return Court.objects.filter(is_active=True).order_by("id").first()

    def _capacity_for_fixed_lesson(fixed_lesson):
        try:
            value = int(fixed_lesson.effective_capacity())
        except Exception:
            value = int(getattr(fixed_lesson, "capacity", 0) or 0)
        return max(value, int(getattr(fixed_lesson, "capacity", 0) or 0), 1)

    def _capacity_for_availability(availability):
        try:
            value = int(availability.effective_capacity())
        except Exception:
            value = int(getattr(availability, "capacity", 0) or 0)
        return max(value, int(getattr(availability, "capacity", 0) or 0), 1)

    target_year, target_month = _parse_target_month(request.GET.get("year"), request.GET.get("month"))
    back_url = f"{reverse('club:lesson_calendar')}?{urlencode({'year': target_year, 'month': target_month})}"

    profile_redirect = _require_profile_completed_for_booking(request)
    if profile_redirect:
        return profile_redirect

    survey_redirect = _require_schedule_survey(request)
    if survey_redirect:
        return survey_redirect

    availability_id = (request.GET.get("availability_id") or "").strip()
    fixed_lesson_id = (request.GET.get("fixed_lesson_id") or "").strip()
    lesson_date_text = (request.GET.get("lesson_date") or "").strip()

    selected_lesson = None

    try:
        fixed_lesson = None
        availability = None
        start_at = None
        end_at = None
        lesson_type = ""
        target_level = ""
        target_level_2 = ""
        coach = None
        substitute_coach = None
        court = None
        coach_name = "-"
        lesson_type_label = "-"
        target_level_label = "-"
        capacity = 1
        status = CoachAvailability.STATUS_OPEN
        source_kind = ""

        if fixed_lesson_id and lesson_date_text:
            fixed_lesson = get_object_or_404(
                FixedLesson.objects.select_related("coach", "coach_2", "coach_3", "court"),
                pk=fixed_lesson_id,
                is_active=True,
            )
            try:
                target_date = date.fromisoformat(lesson_date_text)
            except Exception:
                raise ValidationError("予約対象日が正しくありません。")

            repeat_start = getattr(fixed_lesson, "start_date", None)
            if repeat_start and target_date < repeat_start:
                raise ValidationError("この固定レッスンはまだ開始前です。")

            start_at, end_at = fixed_lesson._build_datetimes_for_date(target_date)
            primary_coach = fixed_lesson.primary_coach() if hasattr(fixed_lesson, "primary_coach") else fixed_lesson.coach
            court = fixed_lesson.court or _first_active_court()

            availability = (
                CoachAvailability.objects.select_related("coach", "substitute_coach", "court")
                .filter(
                    coach=primary_coach,
                    lesson_type=fixed_lesson.lesson_type,
                    start_at=start_at,
                    end_at=end_at,
                )
                .order_by("id")
                .first()
            )
            if availability:
                court = availability.court
                capacity = _capacity_for_availability(availability)
                status = availability.status
                coach = availability.coach
                substitute_coach = availability.substitute_coach
                target_level = availability.target_level
                target_level_2 = getattr(availability, "target_level_2", "") or ""
            else:
                capacity = _capacity_for_fixed_lesson(fixed_lesson)
                status = CoachAvailability.STATUS_OPEN
                coach = primary_coach
                substitute_coach = None
                target_level = fixed_lesson.target_level
                target_level_2 = getattr(fixed_lesson, "target_level_2", "") or ""

            lesson_type = fixed_lesson.lesson_type
            lesson_type_label = fixed_lesson.get_lesson_type_display()
            target_level_label = _lesson_level_label(fixed_lesson) or fixed_lesson.get_target_level_display()
            coach_name = _fixed_lesson_coach_names(fixed_lesson)
            source_kind = "fixed_lesson"

        elif availability_id:
            availability = get_object_or_404(
                CoachAvailability.objects.select_related("coach", "substitute_coach", "court"),
                pk=availability_id,
                lesson_type__in=[Reservation.LESSON_GENERAL, Reservation.LESSON_EVENT],
            )
            start_at = availability.start_at
            end_at = availability.end_at
            lesson_type = availability.lesson_type
            lesson_type_label = availability.get_lesson_type_display()
            target_level = availability.target_level
            target_level_2 = getattr(availability, "target_level_2", "") or ""
            target_level_label = _lesson_level_label(availability) or availability.get_target_level_display()
            coach = availability.coach
            substitute_coach = availability.substitute_coach
            assigned_coach = availability.assigned_coach() if hasattr(availability, "assigned_coach") else (substitute_coach or coach)
            coach_name = _display_name(assigned_coach)
            court = availability.court
            capacity = _capacity_for_availability(availability)
            status = availability.status
            source_kind = "availability"

        else:
            raise ValidationError("対象のレッスンが見つかりません。")

        if not court:
            raise ValidationError("予約に利用できるコートが登録されていません。")

        active_count = Reservation.objects.filter(
            coach=coach,
            court=court,
            lesson_type=lesson_type,
            start_at=start_at,
            end_at=end_at,
            status=Reservation.STATUS_ACTIVE,
        ).count()

        if fixed_lesson is not None:
            try:
                fixed_member_count = fixed_lesson.members.count()
            except Exception:
                fixed_member_count = 0
            active_count = max(int(active_count or 0), int(fixed_member_count or 0))

        waitlist_count = LessonWaitlist.objects.filter(
            coach=coach,
            court=court,
            lesson_type=lesson_type,
            start_at=start_at,
            end_at=end_at,
            status=LessonWaitlist.STATUS_WAITING,
        ).count()

        existing_waitlist = LessonWaitlist.objects.filter(
            user=request.user,
            coach=coach,
            court=court,
            lesson_type=lesson_type,
            start_at=start_at,
            end_at=end_at,
            status=LessonWaitlist.STATUS_WAITING,
        ).first()

        user_slot_status = ""
        own_reservation = Reservation.objects.filter(
            user=request.user,
            coach=coach,
            court=court,
            lesson_type=lesson_type,
            start_at=start_at,
            end_at=end_at,
            status__in=[Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING],
        ).order_by("id").first()
        if own_reservation:
            user_slot_status = own_reservation.status

        participant_choices = build_participant_choices_for_user(
            request.user,
            target_level,
            target_level_2,
        )
        has_bookable_participant = any(choice.get("can_book") for choice in participant_choices)

        can_submit = False
        can_join_waitlist = False
        can_cancel_waitlist = False
        disabled_reason = ""

        is_own_coach_slot = False
        if getattr(coach, "pk", None) == request.user.pk:
            is_own_coach_slot = True
        if getattr(substitute_coach, "pk", None) == request.user.pk:
            is_own_coach_slot = True
        if fixed_lesson is not None and hasattr(fixed_lesson, "includes_coach") and fixed_lesson.includes_coach(request.user):
            is_own_coach_slot = True

        if start_at < timezone.now():
            disabled_reason = "このレッスンは受付終了です。"
        elif status != CoachAvailability.STATUS_OPEN and source_kind != "fixed_lesson":
            disabled_reason = "このレッスンはまだ受付準備中です。"
        elif lesson_type not in (Reservation.LESSON_GENERAL, Reservation.LESSON_EVENT):
            disabled_reason = "このレッスンは個別相談フォームから申請してください。"
        elif not _can_user_take_lessons(request.user):
            disabled_reason = "会員または業務委託コーチアカウントで予約できます。"
        elif is_own_coach_slot:
            disabled_reason = "自分自身が担当するレッスンは予約できません。"
        elif user_slot_status == Reservation.STATUS_ACTIVE:
            disabled_reason = "このレッスンは予約済みです。"
        elif user_slot_status == Reservation.STATUS_PENDING:
            disabled_reason = "この時間帯に承認待ちの申請があります。"
        elif not has_bookable_participant:
            disabled_reason = "このレッスンを予約できる参加者がいません。参加者のレベルを確認してください。"
        elif int(active_count or 0) >= int(capacity or 0):
            if existing_waitlist:
                disabled_reason = "このレッスンはキャンセル待ち登録済みです。"
                can_cancel_waitlist = True
            else:
                disabled_reason = "このレッスンは満員です。キャンセル待ち登録ができます。"
                can_join_waitlist = True
        else:
            can_submit = True

        start_local = _local(start_at)
        end_local = _local(end_at)
        weekday_label = ["月", "火", "水", "木", "金", "土", "日"][start_local.date().weekday()]

        selected_lesson = {
            "availability_id": str(availability.pk) if availability else "",
            "fixed_lesson_id": str(fixed_lesson.pk) if fixed_lesson else "",
            "lesson_date": lesson_date_text if fixed_lesson else "",
            "date_label": f"{start_local:%Y/%m/%d}（{weekday_label}）",
            "time_label": f"{start_local:%H:%M}〜{end_local:%H:%M}",
            "lesson_type_label": lesson_type_label,
            "target_level_label": target_level_label,
            "coach_name": coach_name,
            "court_name": str(court),
            "member_count": int(active_count or 0),
            "capacity": int(capacity or 0),
            "remaining_count": max(int(capacity or 0) - int(active_count or 0), 0),
            "waitlist_count": int(waitlist_count or 0),
            "ticket_label": _regular_lesson_payment_label(lesson_type, start_at),
            "confirm_note": _regular_lesson_confirm_note(lesson_type, start_at),
            "participant_choices": participant_choices,
            "can_submit": can_submit,
            "can_join_waitlist": can_join_waitlist,
            "can_cancel_waitlist": can_cancel_waitlist,
            "disabled_reason": disabled_reason,
        }

    except ValidationError as e:
        message_text = "レッスン情報を取得できませんでした。"
        if hasattr(e, "messages") and e.messages:
            message_text = e.messages[0]
        else:
            message_text = str(e)
        selected_lesson = {
            "disabled_reason": message_text,
            "can_submit": False,
            "can_join_waitlist": False,
            "can_cancel_waitlist": False,
        }
    except Exception as e:
        selected_lesson = {
            "disabled_reason": f"レッスン情報の取得中にエラーが発生しました: {e}",
            "can_submit": False,
            "can_join_waitlist": False,
            "can_cancel_waitlist": False,
        }

    return render(
        request,
        "reservations/regular_lesson_confirm.html",
        {
            "selected_lesson": selected_lesson,
            "target_year": target_year,
            "target_month": target_month,
            "back_url": back_url,
        },
    )


def home(request):
    if request.user.is_authenticated:
        if _needs_profile_completion(request.user):
            return redirect("club:profile_complete")

        survey_redirect = _require_schedule_survey(request)
        if survey_redirect:
            return survey_redirect

        _sync_fixed_lessons()

    User = get_user_model()
    coaches = User.objects.filter(role__in=("coach", "contractor_coach")).order_by("username")
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
                f"ガット張り依頼を受け付けました。料金は {order.total_price()}円 です。"
            )
            return redirect("club:stringing_order_create")

        messages.error(request, "ガット張り依頼を保存できませんでした。入力内容をご確認ください。")

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

    if not _is_staff_like(request.user) and not _is_coach_user(request.user):
        return redirect("club:stringing_order_create")

    queryset = StringingOrder.objects.select_related("user", "assigned_coach").all().order_by("-created_at", "-id")

    status_filter = (request.GET.get("status_filter") or "all").strip()
    valid_status_filters = {
        "all",
        "pending",
        StringingOrder.STATUS_REQUESTED,
        StringingOrder.STATUS_IN_PROGRESS,
        StringingOrder.STATUS_COMPLETED,
        StringingOrder.STATUS_CANCELED,
    }
    if status_filter not in valid_status_filters:
        status_filter = "all"

    all_orders = list(queryset)

    def _filter_orders(order_list, current_filter):
        if current_filter == "all":
            return order_list
        if current_filter == "pending":
            return [
                order for order in order_list
                if getattr(order, "status", "") in (
                    StringingOrder.STATUS_REQUESTED,
                    StringingOrder.STATUS_IN_PROGRESS,
                )
            ]
        return [order for order in order_list if getattr(order, "status", "") == current_filter]

    filtered_orders = _filter_orders(all_orders, status_filter)

    status_counts = {
        "all": len(all_orders),
        "pending": len(_filter_orders(all_orders, "pending")),
        "requested": len(_filter_orders(all_orders, StringingOrder.STATUS_REQUESTED)),
        "in_progress": len(_filter_orders(all_orders, StringingOrder.STATUS_IN_PROGRESS)),
        "completed": len(_filter_orders(all_orders, StringingOrder.STATUS_COMPLETED)),
        "canceled": len(_filter_orders(all_orders, StringingOrder.STATUS_CANCELED)),
    }

    order_rows = []
    for order in filtered_orders:
        preferred_finish_date = getattr(order, "preferred_finish_date", None)
        if preferred_finish_date:
            try:
                preferred_finish_date_label = preferred_finish_date.strftime("%Y-%m-%d")
            except Exception:
                preferred_finish_date_label = str(preferred_finish_date)
        else:
            preferred_finish_date_label = "-"

        order_rows.append(
            {
                "order": order,
                "status_label": _stringing_status_label(order),
                "preferred_finish_date": preferred_finish_date_label,
                "total_price": order.total_price(),
                "can_manage": _is_staff_like(request.user) or getattr(order, "assigned_coach_id", None) == request.user.pk,
            }
        )

    return render(
        request,
        "stringing/list.html",
        {
            "stringing_orders": queryset,
            "order_rows": order_rows,
            "status_filter": status_filter,
            "status_counts": status_counts,
            "stringing_status_choices": StringingOrder.STATUS_CHOICES,
            "is_stringing_manage_mode": True,
            "stringing_base_price": 1200,
            "stringing_delivery_fee": 500,
            "stringing_total_with_delivery": 1700,
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




def _activity_item(
    *,
    created_at,
    action_label,
    result_label,
    title,
    message="",
    actor_name="-",
    target_name="-",
    detail_url="",
    related_label="-",
):
    return {
        "created_at": created_at,
        "action_label": action_label,
        "result_label": result_label,
        "title": title,
        "message": message,
        "actor_name": actor_name,
        "target_name": target_name,
        "detail_url": detail_url,
        "related_label": related_label,
    }


@login_required
@require_GET
def coach_activity_log(request):
    if not (_is_coach_user(request.user) or _is_staff_like(request.user)):
        return HttpResponse("Forbidden", status=403)

    type_filter = (request.GET.get("type") or "all").strip()
    valid_type_filters = {"all", "reservation", "stringing", "ticket"}
    if type_filter not in valid_type_filters:
        type_filter = "all"

    rows = []

    include_reservation = type_filter in ("all", "reservation")
    include_stringing = type_filter in ("all", "stringing")
    include_ticket = type_filter in ("all", "ticket")

    if include_reservation:
        reservation_qs = (
            Reservation.objects.select_related("user", "coach", "substitute_coach", "court")
            .order_by("-created_at", "-id")[:80]
        )

        if _is_coach_user(request.user) and not _is_staff_like(request.user):
            reservation_qs = [
                reservation for reservation in reservation_qs
                if reservation.coach_id == request.user.pk
                or getattr(reservation, "substitute_coach_id", None) == request.user.pk
            ]

        for reservation in reservation_qs:
            if reservation.status == Reservation.STATUS_PENDING:
                action_label = "予約申請"
                result_label = "承認待ち"
                title = "予約申請が作成されました"
            elif reservation.status == Reservation.STATUS_ACTIVE:
                action_label = "予約"
                result_label = "予約中"
                title = "予約が成立しています"
            elif reservation.status == Reservation.STATUS_RAIN_CANCELED:
                action_label = "雨天中止"
                result_label = "中止"
                title = "予約が雨天中止になりました"
            elif reservation.status == Reservation.STATUS_CANCELED:
                action_label = "キャンセル"
                result_label = "キャンセル"
                title = "予約がキャンセルされました"
            else:
                action_label = "予約"
                result_label = reservation.get_status_display()
                title = "予約状態が更新されました"

            rows.append(
                _activity_item(
                    created_at=reservation.canceled_at or reservation.created_at,
                    action_label=action_label,
                    result_label=result_label,
                    title=title,
                    message=(
                        f"{reservation.start_at:%Y-%m-%d %H:%M}〜{reservation.end_at:%H:%M} / "
                        f"{reservation.get_lesson_type_display()} / "
                        f"会員: {_display_name(reservation.user)}"
                    ),
                    actor_name=_display_name(reservation.coach),
                    target_name=_display_name(reservation.user),
                    detail_url=reverse("club:reservation_detail", kwargs={"pk": reservation.pk}),
                    related_label=f"予約ID {reservation.pk}",
                )
            )

    if include_stringing:
        stringing_qs = (
            StringingOrder.objects.select_related("user", "assigned_coach")
            .order_by("-updated_at", "-created_at", "-id")[:80]
        )

        if _is_coach_user(request.user) and not _is_staff_like(request.user):
            stringing_qs = stringing_qs.filter(assigned_coach=request.user)

        for order in stringing_qs:
            rows.append(
                _activity_item(
                    created_at=order.updated_at or order.created_at,
                    action_label="ガット張り",
                    result_label=order.get_status_display(),
                    title="ガット張り依頼の状態",
                    message=(
                        f"会員: {_display_name(order.user)} / "
                        f"ラケット: {order.racket_name or '-'} / "
                        f"ガット: {order.string_name or '-'} / "
                        f"料金: {order.total_price()}円"
                    ),
                    actor_name=_display_name(order.assigned_coach),
                    target_name=_display_name(order.user),
                    detail_url=reverse("club:stringing_order_detail", kwargs={"pk": order.pk}),
                    related_label=f"ガットID {order.pk}",
                )
            )

    if include_ticket:
        ticket_qs = (
            TicketLedger.objects.select_related("user", "created_by", "reservation", "fixed_lesson")
            .order_by("-created_at", "-id")[:80]
        )

        if _is_coach_user(request.user) and not _is_staff_like(request.user):
            # コーチは全チケット履歴ではなく、関連予約に紐づくものを中心に確認
            ticket_qs = ticket_qs.filter(
                reservation__coach=request.user
            ) | ticket_qs.filter(
                reservation__substitute_coach=request.user
            )

        for ledger in ticket_qs[:80]:
            detail_url = ""
            related_label = "-"
            if ledger.reservation_id:
                detail_url = reverse("club:reservation_detail", kwargs={"pk": ledger.reservation_id})
                related_label = f"予約ID {ledger.reservation_id}"
            elif ledger.fixed_lesson_id:
                related_label = f"固定レッスンID {ledger.fixed_lesson_id}"

            sign = "+" if int(ledger.change_amount or 0) >= 0 else ""
            rows.append(
                _activity_item(
                    created_at=ledger.created_at,
                    action_label="チケット",
                    result_label=ledger.get_reason_display(),
                    title=f"チケット {sign}{ledger.change_amount}枚",
                    message=f"残数: {ledger.balance_after}枚 / メモ: {ledger.note or '-'}",
                    actor_name=_display_name(ledger.created_by),
                    target_name=_display_name(ledger.user),
                    detail_url=detail_url,
                    related_label=related_label,
                )
            )

    rows = sorted(
        [row for row in rows if row.get("created_at")],
        key=lambda row: row["created_at"],
        reverse=True,
    )[:100]

    count_map = {
        "all": len(rows),
        "reservation": 0,
        "stringing": 0,
        "ticket": 0,
    }
    for row in rows:
        if row["action_label"] in ("予約申請", "予約", "雨天中止", "キャンセル"):
            count_map["reservation"] += 1
        elif row["action_label"] == "ガット張り":
            count_map["stringing"] += 1
        elif row["action_label"] == "チケット":
            count_map["ticket"] += 1

    return render(
        request,
        "coach/activity_log.html",
        {
            "activity_rows": rows,
            "type_filter": type_filter,
            "count_map": count_map,
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
@require_http_methods(["GET", "POST"])
def coach_today_lessons(request):
    if not (_is_coach_user(request.user) or _is_staff_like(request.user)):
        return HttpResponse("Forbidden", status=403)

    User = get_user_model()
    today = timezone.localdate()

    try:
        display_days = int(request.GET.get("days") or 7)
    except Exception:
        display_days = 7
    if display_days not in (1, 7, 14, 28):
        display_days = 7

    # 「過去1か月」は、今日を含む直近28日間を表示します。
    # 今日・7日間・14日間はこれまでどおり未来の予定確認用です。
    if display_days == 28:
        range_start = today - timedelta(days=27)
        range_end = today
    else:
        range_start = today
        range_end = today + timedelta(days=display_days - 1)

    coach_queryset = User.objects.filter(role__in=("coach", "contractor_coach")).order_by("full_name", "username", "id")

    if _is_coach_user(request.user):
        selected_coach = request.user
        selected_coach_id = str(request.user.pk)
        is_staff_mode = False
    else:
        selected_coach_id = (request.GET.get("coach_id") or "").strip()
        selected_coach = (
            coach_queryset.filter(pk=selected_coach_id).first()
            if selected_coach_id
            else coach_queryset.first()
        )
        selected_coach_id = str(selected_coach.pk) if selected_coach else ""
        is_staff_mode = True

    def _today_lessons_redirect():
        params = {"days": display_days}
        if selected_coach_id:
            params["coach_id"] = selected_coach_id
        return f"{reverse('club:coach_today_lessons')}?{urlencode(params)}"

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action == "update_payment":
            reservation_id = (request.POST.get("reservation_id") or "").strip()
            payment_status = (request.POST.get("payment_status") or "").strip()

            reservation = get_object_or_404(
                Reservation.objects.select_related("user", "coach", "substitute_coach", "court"),
                pk=reservation_id,
            )

            can_update = (
                _is_staff_like(request.user)
                or reservation.coach_id == request.user.pk
                or getattr(reservation, "substitute_coach_id", None) == request.user.pk
            )
            if not can_update:
                return HttpResponse("Forbidden", status=403)

            allowed_statuses = {
                Reservation.PAYMENT_STATUS_UNPAID,
                Reservation.PAYMENT_STATUS_PAID,
                Reservation.PAYMENT_STATUS_WAIVED,
            }
            if payment_status not in allowed_statuses:
                messages.error(request, "支払状況が不正です。")
                return redirect(_today_lessons_redirect())

            try:
                reservation.mark_payment_status(
                    payment_status,
                    received_by=request.user,
                    note="本日の受付・精算画面から更新",
                )
                messages.success(
                    request,
                    f"{_display_name(reservation.user)}さんの参加費状況を「{reservation.get_payment_status_display()}」に更新しました。",
                )
            except ValidationError as e:
                if hasattr(e, "messages"):
                    for message_text in e.messages:
                        messages.error(request, message_text)
                else:
                    messages.error(request, str(e))
            except Exception as e:
                messages.error(request, f"支払状況の更新に失敗しました: {e}")

            return redirect(_today_lessons_redirect())

    slot_map = {}

    def _slot_key_for_row(*, lesson_type, coach_id, court_id, start_at, end_at):
        return _slot_key(lesson_type, coach_id, court_id, start_at, end_at)

    def _local(value):
        if timezone.is_aware(value):
            return timezone.localtime(value)
        return value

    def _safe_phone(user):
        return (getattr(user, "phone_number", "") or "").strip()

    def _safe_level(user):
        try:
            return user.get_member_level_display()
        except Exception:
            return getattr(user, "member_level", "") or "-"

    def _reservation_person_row(reservation):
        payment_status_options = [
            (Reservation.PAYMENT_STATUS_UNPAID, "未回収"),
            (Reservation.PAYMENT_STATUS_PAID, "回収済み"),
            (Reservation.PAYMENT_STATUS_WAIVED, "免除"),
        ]
        return {
            "reservation": reservation,
            "name": _display_name(reservation.user),
            "phone": _safe_phone(reservation.user),
            "level": _safe_level(reservation.user),
            "status_label": reservation.get_status_display(),
            "detail_url": reverse("club:reservation_detail", kwargs={"pk": reservation.pk}),
            "payment_required": reservation.is_payment_tracking_required(),
            "payment_status": reservation.payment_status,
            "payment_status_label": reservation.payment_status_badge_label(),
            "payment_amount": int(reservation.payment_amount or 0),
            "payment_received_at": reservation.payment_received_at,
            "payment_status_options": payment_status_options,
        }

    def _member_person_row(member):
        return {
            "user": member,
            "name": _display_name(member),
            "phone": _safe_phone(member),
            "level": _safe_level(member),
        }

    def _waitlist_person_row(waitlist):
        return {
            "waitlist": waitlist,
            "name": _display_name(waitlist.user),
            "phone": _safe_phone(waitlist.user),
            "level": _safe_level(waitlist.user),
            "created_at": waitlist.created_at,
            "can_promote": _coach_can_manage_waitlist(request.user, waitlist),
        }

    def _fixed_registered_member_rows(fixed_lesson, reservations):
        if not fixed_lesson:
            return []
        reserved_user_ids = {reservation.user_id for reservation in reservations}
        rows = []
        for member in fixed_lesson.members.all().order_by("full_name", "username", "id"):
            if member.pk in reserved_user_ids:
                continue
            rows.append(_member_person_row(member))
        return rows

    def _add_slot(
        *,
        key,
        start_at,
        end_at,
        lesson_type_label,
        target_level_label,
        coach_name,
        court_name,
        capacity,
        title,
        fixed_lesson=None,
        availability=None,
    ):
        if key in slot_map:
            return slot_map[key]

        reservations = list(
            Reservation.objects.select_related("user", "coach", "substitute_coach", "court")
            .filter(
                lesson_type=key[0],
                coach_id=key[1] or None,
                court_id=key[2] or None,
                start_at=start_at,
                end_at=end_at,
                status=Reservation.STATUS_ACTIVE,
            )
            .order_by("user__full_name", "user__username", "id")
        )

        pending_reservations = list(
            Reservation.objects.select_related("user", "coach", "substitute_coach", "court")
            .filter(
                lesson_type=key[0],
                coach_id=key[1] or None,
                court_id=key[2] or None,
                start_at=start_at,
                end_at=end_at,
                status=Reservation.STATUS_PENDING,
            )
            .order_by("user__full_name", "user__username", "id")
        )

        waitlists = list(
            LessonWaitlist.objects.select_related("user", "coach", "substitute_coach", "court")
            .filter(
                lesson_type=key[0],
                coach_id=key[1] or None,
                court_id=key[2] or None,
                start_at=start_at,
                end_at=end_at,
                status=LessonWaitlist.STATUS_WAITING,
            )
            .order_by("created_at", "id")
        )

        start_local = _local(start_at)
        end_local = _local(end_at)
        lesson_date = start_local.date()
        participant_count = len(reservations)
        remaining_count = max(int(capacity or 0) - participant_count, 0)
        is_today = lesson_date == today
        is_past = end_at < timezone.now()
        is_full = participant_count >= int(capacity or 0)
        has_waitlist = bool(waitlists)
        needs_attention = bool(pending_reservations or waitlists or (is_today and remaining_count > 0))

        row = {
            "key": "|".join([str(part) for part in key]),
            "start_at": start_at,
            "end_at": end_at,
            "date": lesson_date,
            "date_label": f"{lesson_date:%Y/%m/%d}",
            "weekday_label": ["月", "火", "水", "木", "金", "土", "日"][lesson_date.weekday()],
            "time_label": f"{start_local:%H:%M}〜{end_local:%H:%M}",
            "title": title,
            "lesson_type_label": lesson_type_label,
            "target_level_label": target_level_label,
            "coach_name": coach_name,
            "court_name": court_name,
            "capacity": int(capacity or 0),
            "reservations": reservations,
            "pending_reservations": pending_reservations,
            "waitlists": waitlists,
            "participant_rows": [_reservation_person_row(reservation) for reservation in reservations],
            "pending_rows": [_reservation_person_row(reservation) for reservation in pending_reservations],
            "waitlist_rows": [_waitlist_person_row(waitlist) for waitlist in waitlists],
            "registered_member_rows": _fixed_registered_member_rows(fixed_lesson, reservations),
            "participant_count": participant_count,
            "pending_count": len(pending_reservations),
            "waitlist_count": len(waitlists),
            "remaining_count": remaining_count,
            "is_today": is_today,
            "is_past": is_past,
            "is_full": is_full,
            "has_waitlist": has_waitlist,
            "needs_attention": needs_attention,
            "status_label": "本日" if is_today else ("終了" if is_past else "予定"),
            "fixed_lesson": fixed_lesson,
            "availability": availability,
        }
        slot_map[key] = row
        return row

    def _availability_capacity(availability):
        try:
            return max(int(availability.effective_capacity()), int(availability.capacity or 0), 1)
        except Exception:
            return max(int(getattr(availability, "capacity", 1) or 1), 1)

    fixed_queryset = (
        FixedLesson.objects.filter(is_active=True)
        .select_related("coach", "coach_2", "coach_3", "court")
        .prefetch_related("members")
        .order_by("weekday", "start_hour", "id")
    )
    if selected_coach is not None:
        fixed_queryset = [fixed for fixed in fixed_queryset if _fixed_lesson_includes_coach(fixed, selected_coach)]

    cursor = range_start
    while cursor <= range_end:
        for fixed in fixed_queryset:
            if int(fixed.weekday) != int(cursor.weekday()):
                continue
            repeat_start = getattr(fixed, "start_date", None)
            if repeat_start and cursor < repeat_start:
                continue

            try:
                start_at, end_at = fixed._build_datetimes_for_date(cursor)
            except Exception:
                continue

            primary_coach = fixed.primary_coach() if hasattr(fixed, "primary_coach") else fixed.coach
            court = fixed.court
            if not court:
                continue

            availability = (
                CoachAvailability.objects.filter(
                    coach=primary_coach,
                    court=court,
                    lesson_type=fixed.lesson_type,
                    start_at=start_at,
                    end_at=end_at,
                )
                .select_related("coach", "substitute_coach", "court")
                .first()
            )

            capacity = fixed.effective_capacity() if hasattr(fixed, "effective_capacity") else fixed.capacity
            if availability:
                try:
                    capacity = max(
                        int(availability.effective_capacity()),
                        int(availability.capacity or 0),
                        int(capacity or 0),
                        1,
                    )
                except Exception:
                    capacity = max(int(availability.capacity or 0), int(capacity or 0), 1)

            key = _slot_key_for_row(
                lesson_type=fixed.lesson_type,
                coach_id=getattr(primary_coach, "pk", None),
                court_id=getattr(court, "pk", None),
                start_at=start_at,
                end_at=end_at,
            )

            _add_slot(
                key=key,
                start_at=start_at,
                end_at=end_at,
                lesson_type_label=fixed.get_lesson_type_display(),
                target_level_label=_lesson_level_label(fixed) or fixed.get_target_level_display(),
                coach_name=_fixed_lesson_coach_names(fixed),
                court_name=str(court),
                capacity=capacity,
                title=_lesson_calendar_title(fixed),
                fixed_lesson=fixed,
                availability=availability,
            )

        cursor += timedelta(days=1)

    availability_qs = (
        CoachAvailability.objects.filter(
            start_at__date__gte=range_start,
            start_at__date__lte=range_end,
        )
        .select_related("coach", "substitute_coach", "court")
        .order_by("start_at", "id")
    )

    if selected_coach is not None:
        availability_qs = [
            availability for availability in availability_qs
            if availability.coach_id == selected_coach.pk
            or getattr(availability, "substitute_coach_id", None) == selected_coach.pk
        ]

    for availability in availability_qs:
        key = _slot_key_for_row(
            lesson_type=availability.lesson_type,
            coach_id=availability.coach_id,
            court_id=availability.court_id,
            start_at=availability.start_at,
            end_at=availability.end_at,
        )
        if key in slot_map:
            continue

        capacity = _availability_capacity(availability)
        assigned_coach = (
            availability.assigned_coach()
            if hasattr(availability, "assigned_coach")
            else (availability.substitute_coach or availability.coach)
        )

        _add_slot(
            key=key,
            start_at=availability.start_at,
            end_at=availability.end_at,
            lesson_type_label=availability.get_lesson_type_display(),
            target_level_label=_lesson_level_label(availability) or availability.get_target_level_display(),
            coach_name=_display_name(assigned_coach),
            court_name=str(availability.court),
            capacity=capacity,
            title=availability.get_lesson_type_display(),
            availability=availability,
        )

    lesson_rows = sorted(slot_map.values(), key=lambda row: (row["start_at"], row["title"], row["key"]))
    today_rows = [row for row in lesson_rows if row["date"] == today]
    upcoming_rows = [row for row in lesson_rows if row["date"] != today]
    attention_rows = [row for row in lesson_rows if row["needs_attention"] and not row["is_past"]]

    # 過去1か月表示では、終了済みレッスンも含めて参加費の回収状況を編集できるようにします。
    for row in lesson_rows:
        payment_rows = [
            person for person in row["participant_rows"]
            if person.get("payment_required")
        ]
        row["payment_target_count"] = len(payment_rows)
        row["payment_unpaid_count"] = sum(
            1 for person in payment_rows
            if person.get("payment_status") == Reservation.PAYMENT_STATUS_UNPAID
        )

    grouped_days = []
    day_cursor = range_start
    while day_cursor <= range_end:
        day_rows = [row for row in lesson_rows if row["date"] == day_cursor]
        grouped_days.append(
            {
                "date": day_cursor,
                "date_label": f"{day_cursor:%Y/%m/%d}",
                "weekday_label": ["月", "火", "水", "木", "金", "土", "日"][day_cursor.weekday()],
                "is_today": day_cursor == today,
                "rows": day_rows,
            }
        )
        day_cursor += timedelta(days=1)

    all_active_reservations = []
    for row in lesson_rows:
        all_active_reservations.extend(row["reservations"])

    payment_target_reservations = [
        reservation for reservation in all_active_reservations
        if reservation.is_payment_tracking_required()
    ]
    payment_paid_total = sum(
        int(reservation.payment_amount or 0)
        for reservation in payment_target_reservations
        if reservation.payment_status == Reservation.PAYMENT_STATUS_PAID
    )
    payment_unpaid_total = sum(
        int(reservation.payment_amount or 0)
        for reservation in payment_target_reservations
        if reservation.payment_status == Reservation.PAYMENT_STATUS_UNPAID
    )
    payment_waived_total = sum(
        int(reservation.payment_amount or 0)
        for reservation in payment_target_reservations
        if reservation.payment_status == Reservation.PAYMENT_STATUS_WAIVED
    )

    summary = {
        "lesson_count": len(lesson_rows),
        "today_lesson_count": len(today_rows),
        "participant_count": sum(row["participant_count"] for row in lesson_rows),
        "today_participant_count": sum(row["participant_count"] for row in today_rows),
        "waitlist_count": sum(row["waitlist_count"] for row in lesson_rows),
        "pending_count": sum(row["pending_count"] for row in lesson_rows),
        "attention_count": len(attention_rows),
        "payment_target_count": len(payment_target_reservations),
        "payment_paid_total": payment_paid_total,
        "payment_unpaid_total": payment_unpaid_total,
        "payment_waived_total": payment_waived_total,
    }

    return render(
        request,
        "coach/today_lessons.html",
        {
            "coach_options": coach_queryset,
            "selected_coach": selected_coach,
            "selected_coach_id": selected_coach_id,
            "is_staff_mode": is_staff_mode,
            "display_days": display_days,
            "range_start": range_start,
            "range_end": range_end,
            "grouped_days": grouped_days,
            "lesson_rows": lesson_rows,
            "today_rows": today_rows,
            "upcoming_rows": upcoming_rows,
            "attention_rows": attention_rows[:10],
            "summary": summary,
        },
    )


@login_required
@require_GET
def coach_fixed_lesson_weekly(request):
    if not (_is_coach_user(request.user) or _is_staff_like(request.user)):
        return HttpResponse("Forbidden", status=403)

    User = get_user_model()
    today = timezone.localdate()
    coach_queryset = User.objects.filter(role__in=("coach", "contractor_coach")).order_by("full_name", "username", "id")

    # コーチ本人は staff 権限の有無にかかわらず、必ず自分の担当スケジュールを表示する。
    # 管理者だけが coach_id で表示対象コーチを切り替えられる。
    if _is_coach_user(request.user):
        selected_coach = request.user
        selected_coach_id = str(request.user.pk)
        is_staff_mode = False
    else:
        selected_coach_id = (request.GET.get("coach_id") or "").strip()
        selected_coach = (
            coach_queryset.filter(pk=selected_coach_id).first() if selected_coach_id else coach_queryset.first()
        )
        selected_coach_id = str(selected_coach.pk) if selected_coach else ""
        is_staff_mode = True

    display_weeks = 12
    week_start, week_end = _week_range_for_display(today)
    display_until = today + timedelta(days=display_weeks * 7)

    fixed_lessons = []
    fixed_queryset = (
        FixedLesson.objects.filter(is_active=True)
        .select_related("coach", "coach_2", "coach_3", "court")
        .prefetch_related("members")
        .order_by("weekday", "start_hour", "id")
    )

    if selected_coach is not None:
        fixed_queryset = [fixed for fixed in fixed_queryset if _fixed_lesson_includes_coach(fixed, selected_coach)]

    weekday_labels = dict(FixedLesson.WEEKDAY_CHOICES)

    for fixed in fixed_queryset:
        members = list(fixed.members.all().order_by("full_name", "username", "id"))
        repeat_start = getattr(fixed, "start_date", None) or today
        if repeat_start < today:
            repeat_start = today

        initial_offset = (int(fixed.weekday) - repeat_start.weekday()) % 7

        for week_index in range(max(int(getattr(fixed, "weeks_ahead", 8) or 8), 1)):
            target_date = repeat_start + timedelta(days=initial_offset + (7 * week_index))
            if target_date > display_until:
                break

            start_at, end_at = fixed._build_datetimes_for_date(target_date)

            slot_availability = (
                CoachAvailability.objects.filter(
                    coach=fixed.primary_coach() if hasattr(fixed, "primary_coach") else fixed.coach,
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
                    status=Reservation.STATUS_ACTIVE,
                )
                .select_related("user", "coach", "substitute_coach", "court")
                .order_by("user__full_name", "user__username", "id")
            )

            waitlist_count = 0
            try:
                waitlist_count = LessonWaitlist.objects.filter(
                    fixed_lesson=fixed,
                    start_at=start_at,
                    end_at=end_at,
                    status=LessonWaitlist.STATUS_WAITING,
                ).count()
            except Exception:
                waitlist_count = 0

            member_names = [member.display_name() for member in members]
            reservation_names = [reservation.user.display_name() for reservation in week_reservations]
            assigned_coach = (
                slot_availability.substitute_coach
                if slot_availability and slot_availability.substitute_coach
                else (fixed.primary_coach() if hasattr(fixed, "primary_coach") else fixed.coach)
            )

            fixed_lessons.append(
                {
                    "fixed_lesson": fixed,
                    "weekday_label": weekday_labels.get(fixed.weekday, str(fixed.weekday)),
                    "target_date": target_date,
                    "start_at": start_at,
                    "end_at": end_at,
                    "assigned_coach_name": _display_name(assigned_coach),
                    "normal_coach_name": _fixed_lesson_coach_names(fixed),
                    "substitute_coach_name": _display_name(slot_availability.substitute_coach)
                    if slot_availability and slot_availability.substitute_coach
                    else "",
                    "has_substitute": bool(slot_availability and slot_availability.substitute_coach),
                    "member_count": len(member_names),
                    "member_names": member_names,
                    "reservation_count": max(len(reservation_names), len(member_names)),
                    "reservation_names": reservation_names,
                    "waitlist_count": waitlist_count,
                    "capacity": fixed.effective_capacity() if hasattr(fixed, "effective_capacity") else fixed.capacity,
                    "slot_availability": slot_availability,
                }
            )

    fixed_lessons = sorted(fixed_lessons, key=lambda row: (row["target_date"], row["start_at"], row["fixed_lesson"].id))

    return render(
        request,
        "coach/fixed_lesson_weekly.html",
        {
            "coach_options": coach_queryset,
            "selected_coach": selected_coach,
            "selected_coach_id": selected_coach_id,
            "fixed_lessons": fixed_lessons,
            "week_start": today,
            "week_end": display_until,
            "week_label": f"{today:%Y-%m-%d} 〜 {display_until:%Y-%m-%d}",
            "display_weeks": display_weeks,
            "is_staff_mode": is_staff_mode,
        },
    )




def _user_can_access_stringing_order(user, order):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if _is_staff_like(user):
        return True
    if getattr(order, "user_id", None) == getattr(user, "pk", None):
        return True
    if getattr(order, "assigned_coach_id", None) == getattr(user, "pk", None):
        return True
    return False


def _user_can_manage_stringing_order(user, order):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if _is_staff_like(user):
        return True
    if _is_coach_user(user) and getattr(order, "assigned_coach_id", None) == getattr(user, "pk", None):
        return True
    return False


@login_required
@require_GET
def reservation_detail(request, pk):
    reservation = get_object_or_404(
        Reservation.objects.select_related(
            "user",
            "coach",
            "substitute_coach",
            "court",
            "availability",
            "fixed_lesson",
        ).prefetch_related("ticket_consumptions__purchase", "ticket_ledgers"),
        pk=pk,
    )

    if not _user_can_access_reservation(request.user, reservation):
        return HttpResponse("Forbidden", status=403)

    can_cancel, cancel_reason = _can_user_cancel_reservation(request.user, reservation)
    can_manage_request = _coach_can_manage_request(request.user, reservation) and reservation.status == Reservation.STATUS_PENDING

    ticket_consumption_rows = []
    for consumption in reservation.ticket_consumptions.select_related("purchase").order_by("created_at", "id"):
        ticket_consumption_rows.append(
            {
                "consumption": consumption,
                "unit_price_label": consumption.unit_price_label(),
                "is_refunded": bool(consumption.refunded_at),
            }
        )

    ticket_ledger_rows = list(
        reservation.ticket_ledgers.select_related("created_by").order_by("-created_at", "-id")[:20]
    )

    same_slot_reservations = list(
        Reservation.objects.select_related("user", "coach", "substitute_coach", "court")
        .filter(
            coach=reservation.coach,
            court=reservation.court,
            lesson_type=reservation.lesson_type,
            start_at=reservation.start_at,
            end_at=reservation.end_at,
            status=Reservation.STATUS_ACTIVE,
        )
        .order_by("user__full_name", "user__username", "id")
    )

    same_slot_waitlists = list(
        LessonWaitlist.objects.select_related("user", "coach", "substitute_coach", "court", "availability", "fixed_lesson")
        .filter(
            coach=reservation.coach,
            court=reservation.court,
            lesson_type=reservation.lesson_type,
            start_at=reservation.start_at,
            end_at=reservation.end_at,
        )
        .order_by("status", "created_at", "id")
    )

    capacity = _capacity_for_reservation_slot(reservation)
    active_count = len(same_slot_reservations)
    waitlist_rows = []
    for waitlist in same_slot_waitlists:
        can_promote = (
            waitlist.status == LessonWaitlist.STATUS_WAITING
            and reservation.start_at >= timezone.now()
            and active_count < capacity
            and _coach_can_manage_waitlist(request.user, waitlist)
        )
        waitlist_rows.append(
            {
                "waitlist": waitlist,
                "can_promote": can_promote,
                "can_cancel": (
                    waitlist.status == LessonWaitlist.STATUS_WAITING
                    and waitlist.start_at >= timezone.now()
                    and _user_can_manage_waitlist(request.user, waitlist)
                ),
            }
        )

    return render(
        request,
        "reservations/detail.html",
        {
            "reservation": reservation,
            "can_cancel": can_cancel,
            "cancel_reason": cancel_reason,
            "can_manage_request": can_manage_request,
            "assigned_coach_name": reservation.assigned_coach_display(),
            "normal_coach_name": reservation.normal_coach_display(),
            "substitute_coach_name": _display_name(reservation.substitute_coach) if reservation.substitute_coach else "",
            "has_substitute": reservation.has_substitute_coach(),
            "ticket_consumption_rows": ticket_consumption_rows,
            "ticket_ledger_rows": ticket_ledger_rows,
            "same_slot_reservations": same_slot_reservations,
            "same_slot_waitlist_rows": waitlist_rows,
            "slot_capacity": capacity,
            "slot_active_count": active_count,
            "slot_remaining_count": max(capacity - active_count, 0),
        },
    )
@require_http_methods(["GET", "POST"])
def stringing_order_detail(request, pk):
    survey_redirect = _require_schedule_survey(request)
    if survey_redirect:
        return survey_redirect

    order = get_object_or_404(
        StringingOrder.objects.select_related("user", "assigned_coach"),
        pk=pk,
    )

    if not _user_can_access_stringing_order(request.user, order):
        return HttpResponse("Forbidden", status=403)

    can_manage = _user_can_manage_stringing_order(request.user, order)

    if request.method == "POST":
        if not can_manage:
            return HttpResponse("Forbidden", status=403)

        new_status = (request.POST.get("new_status") or "").strip()
        valid_statuses = {value for value, _label in StringingOrder.STATUS_CHOICES}

        if new_status not in valid_statuses:
            messages.error(request, "更新する状態が不正です。")
            return redirect("club:stringing_order_detail", pk=order.pk)

        if order.status == new_status:
            messages.info(request, "状態に変更はありません。")
            return redirect("club:stringing_order_detail", pk=order.pk)

        try:
            order.status = new_status
            order.save(update_fields=["status", "updated_at"])
            messages.success(request, f"ガット張り依頼の状態を「{order.get_status_display()}」に更新しました。")
        except Exception as e:
            messages.error(request, f"ガット張り依頼の状態更新に失敗しました: {e}")

        return redirect("club:stringing_order_detail", pk=order.pk)

    preferred_finish_date = getattr(order, "preferred_finish_date", None)
    if preferred_finish_date:
        try:
            preferred_finish_date_label = preferred_finish_date.strftime("%Y-%m-%d")
        except Exception:
            preferred_finish_date_label = str(preferred_finish_date)
    else:
        preferred_finish_date_label = "-"

    return render(
        request,
        "stringing/detail.html",
        {
            "order": order,
            "can_manage": can_manage,
            "status_choices": StringingOrder.STATUS_CHOICES,
            "status_label": _stringing_status_label(order),
            "delivery_label": _stringing_delivery_label(order),
            "preferred_finish_date": preferred_finish_date_label,
            "total_price": order.total_price(),
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

    coach_queryset = User.objects.filter(role__in=("coach", "contractor_coach")).order_by("full_name", "username", "id")
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



EXPENSE_TYPE_PERSONAL = "personal"
EXPENSE_TYPE_COMMON = "common"
EXPENSE_TYPE_SALARY_PAYOUT = "salary_payout"
EXPENSE_TYPE_REIMBURSEMENT_PAYOUT = "reimbursement_payout"

EXPENSE_RECEIPT_NONE = "none"
EXPENSE_RECEIPT_HAS = "has"

EXPENSE_RECEIPT_CHECK_UNCHECKED = "unchecked"
EXPENSE_RECEIPT_CHECK_CHECKED = "checked"

EXPENSE_APPROVAL_DRAFT = "draft"
EXPENSE_APPROVAL_SUBMITTED = "submitted"
EXPENSE_APPROVAL_APPROVED = "approved"
EXPENSE_APPROVAL_RETURNED = "returned"
# 雨天中止でコート運営会社から返金されるまで、通常経費から除外する状態です。
EXPENSE_APPROVAL_REFUND_PENDING = "refund_pending"
EXPENSE_APPROVAL_REFUNDED = "refunded"

EXPENSE_TYPE_CHOICES = (
    (EXPENSE_TYPE_PERSONAL, "本人立替"),
    (EXPENSE_TYPE_COMMON, "共通経費"),
)

EXPENSE_TYPE_LABELS = {
    EXPENSE_TYPE_PERSONAL: "本人立替",
    EXPENSE_TYPE_COMMON: "共通経費",
    EXPENSE_TYPE_SALARY_PAYOUT: "給与支払い",
    EXPENSE_TYPE_REIMBURSEMENT_PAYOUT: "本人立替精算支払い",
}

EXPENSE_PAYOUT_TYPES = {
    EXPENSE_TYPE_SALARY_PAYOUT,
    EXPENSE_TYPE_REIMBURSEMENT_PAYOUT,
}

EXPENSE_RECEIPT_CHOICES = (
    (EXPENSE_RECEIPT_NONE, "なし"),
    (EXPENSE_RECEIPT_HAS, "あり"),
)

EXPENSE_RECEIPT_CHECK_CHOICES = (
    (EXPENSE_RECEIPT_CHECK_UNCHECKED, "未確認"),
    (EXPENSE_RECEIPT_CHECK_CHECKED, "確認済み"),
)

EXPENSE_APPROVAL_CHOICES = (
    (EXPENSE_APPROVAL_DRAFT, "下書き"),
    (EXPENSE_APPROVAL_SUBMITTED, "提出済み"),
    (EXPENSE_APPROVAL_APPROVED, "承認"),
    (EXPENSE_APPROVAL_RETURNED, "差戻し"),
    (EXPENSE_APPROVAL_REFUND_PENDING, "雨天返金待ち"),
    (EXPENSE_APPROVAL_REFUNDED, "雨天返金済み"),
)

EXPENSE_NOTE_META_PREFIX = "__EXPENSE_META__"


def _choice_label(choice_list, value):
    return dict(choice_list).get(value, value)


def _expense_default_meta():
    return {
        "expense_type": EXPENSE_TYPE_COMMON,
        "receipt_status": EXPENSE_RECEIPT_NONE,
        "receipt_check_status": EXPENSE_RECEIPT_CHECK_UNCHECKED,
        "approval_status": EXPENSE_APPROVAL_APPROVED,
    }


def _expense_build_note(raw_note, *, expense_type, receipt_status, receipt_check_status, approval_status, extra_meta=None):
    payload = {
        "expense_type": expense_type,
        "receipt_status": receipt_status,
        "receipt_check_status": receipt_check_status,
        "approval_status": approval_status,
    }
    if extra_meta:
        payload.update(extra_meta)
    clean_note = (raw_note or "").strip()
    return f"{EXPENSE_NOTE_META_PREFIX}{json.dumps(payload, ensure_ascii=False)}\\n{clean_note}"


def _expense_parse_note(stored_note):
    default_meta = _expense_default_meta()
    text = (stored_note or "")
    if not text.startswith(EXPENSE_NOTE_META_PREFIX):
        return {
            **default_meta,
            "plain_note": text.strip(),
        }

    try:
        first_line, plain_note = text.split("\\n", 1)
    except ValueError:
        first_line = text
        plain_note = ""

    meta_json = first_line[len(EXPENSE_NOTE_META_PREFIX):].strip()
    try:
        parsed = json.loads(meta_json or "{}")
    except Exception:
        parsed = {}

    merged = {
        **default_meta,
        **parsed,
        "plain_note": (plain_note or "").strip(),
    }
    return merged


def _expense_is_refund_status(status):
    return status in (
        EXPENSE_APPROVAL_REFUND_PENDING,
        EXPENSE_APPROVAL_REFUNDED,
    )


def _court_facility_key_for_court(court):
    """
    コート番号ではなく、施設単位でコート費用を紐づけます。
    例: 西猪名公園テニスコート / 尼崎記念公園テニスコート
    """
    if not court:
        return "facility:unknown"

    court_type = str(getattr(court, "court_type", "") or "").strip()
    if court_type:
        return f"facility:{court_type}"

    court_name = str(getattr(court, "name", "") or court or "").strip()
    if court_name:
        return f"facility_name:{court_name}"

    return "facility:unknown"


def _court_facility_label_for_court(court):
    if not court:
        return "現地"

    court_type = str(getattr(court, "court_type", "") or "").strip()
    label_map = {
        "sono": "西猪名公園テニスコート",
        "amagasaki": "尼崎記念公園テニスコート",
        "other": "その他テニスコート",
    }
    if court_type in label_map:
        return label_map[court_type]

    court_name = str(getattr(court, "name", "") or court or "").strip()
    return court_name or "現地"


def _court_refund_slot_key(*, lesson_date, start_at, end_at, court):
    start_local = timezone.localtime(start_at) if timezone.is_aware(start_at) else start_at
    end_local = timezone.localtime(end_at) if timezone.is_aware(end_at) else end_at
    if hasattr(lesson_date, "isoformat"):
        date_text = lesson_date.isoformat()
    else:
        date_text = str(lesson_date)
    return (
        f"{date_text}|"
        f"{_court_facility_key_for_court(court)}|"
        f"{start_local:%H:%M}|"
        f"{end_local:%H:%M}"
    )


def _availability_court_refund_slot_key(availability):
    start_local = timezone.localtime(availability.start_at) if timezone.is_aware(availability.start_at) else availability.start_at
    lesson_date = start_local.date()
    return _court_refund_slot_key(
        lesson_date=lesson_date,
        start_at=availability.start_at,
        end_at=availability.end_at,
        court=availability.court,
    )


def _availability_court_refund_lesson_label(availability):
    start_local = timezone.localtime(availability.start_at) if timezone.is_aware(availability.start_at) else availability.start_at
    end_local = timezone.localtime(availability.end_at) if timezone.is_aware(availability.end_at) else availability.end_at
    coach = getattr(availability, "substitute_coach", None) or getattr(availability, "coach", None)
    return (
        f"{start_local:%Y/%m/%d} {start_local:%H:%M}〜{end_local:%H:%M} / "
        f"{_court_facility_label_for_court(getattr(availability, 'court', None))} / "
        f"{_display_name(coach)} / {availability.get_lesson_type_display()}"
    )


def _court_refund_lesson_choices_for_user(user, *, start_date=None, end_date=None):
    qs = CoachAvailability.objects.select_related("coach", "substitute_coach", "court").all()

    if start_date:
        qs = qs.filter(start_at__date__gte=start_date)
    if end_date:
        qs = qs.filter(start_at__date__lte=end_date)

    if _is_coach_user(user) and not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
        qs = qs.filter(Q(coach=user) | Q(substitute_coach=user))

    choices = OrderedDict()
    for availability in qs.order_by("-start_at", "court_id", "coach_id", "id"):
        try:
            key = _availability_court_refund_slot_key(availability)
            if key in choices:
                continue
            choices[key] = {
                "key": key,
                "label": _availability_court_refund_lesson_label(availability),
                "facility_label": _court_facility_label_for_court(availability.court),
            }
        except Exception:
            continue

    return list(choices.values())


def _court_expense_matches_availability(expense, availability):
    """
    雨天中止の対象コート費用を安全に絞り込みます。

    新運用では、経費登録時に選んだ「施設名＋日付＋時間帯」のキーで一致判定します。
    コート番号は直前に決まるため、判定には使いません。
    既存データ救済用として、メモ内の施設名・コート名・court:<ID> でも fallback 判定します。
    """
    if not expense or not availability or expense.category != CoachExpense.CATEGORY_COURT:
        return False

    try:
        lesson_date = timezone.localtime(availability.start_at).date() if timezone.is_aware(availability.start_at) else availability.start_at.date()
    except Exception:
        return False

    if expense.expense_date != lesson_date:
        return False

    meta = _expense_parse_note(expense.note)
    slot_key = str(meta.get("court_refund_slot_key") or "").strip()
    expected_slot_key = _availability_court_refund_slot_key(availability)
    if slot_key and slot_key == expected_slot_key:
        return True

    plain_note = str(meta.get("plain_note") or "")
    facility_label = _court_facility_label_for_court(getattr(availability, "court", None))
    court_name = str(getattr(availability, "court", "") or "").strip()
    court_token = f"court:{getattr(availability, 'court_id', '')}"
    return bool(
        (facility_label and facility_label in plain_note)
        or (court_name and court_name in plain_note)
        or (getattr(availability, "court_id", None) and court_token in plain_note)
    )


def _mark_court_expenses_refund_pending_for_rain_cancel(availability, *, changed_by=None):
    """
    承認済みの対象コート費用を、雨天返金待ちへ自動差戻しします。
    返金待ち・返金済み・未承認の経費は変更しません。
    """
    if not availability:
        return 0

    try:
        lesson_date = timezone.localtime(availability.start_at).date() if timezone.is_aware(availability.start_at) else availability.start_at.date()
    except Exception:
        return 0

    changed_count = 0
    court_expenses = CoachExpense.objects.filter(
        expense_date=lesson_date,
        category=CoachExpense.CATEGORY_COURT,
    ).order_by("id")

    for expense in court_expenses:
        meta = _expense_parse_note(expense.note)
        if meta.get("approval_status") != EXPENSE_APPROVAL_APPROVED:
            continue
        if not _court_expense_matches_availability(expense, availability):
            continue

        extra_meta = {
            key: value
            for key, value in meta.items()
            if key not in {
                "expense_type",
                "receipt_status",
                "receipt_check_status",
                "approval_status",
                "plain_note",
            }
        }
        extra_meta.update(
            {
                "rain_canceled_at": timezone.now().isoformat(),
                "rain_canceled_by_id": getattr(changed_by, "pk", None),
                "rain_canceled_by_name": _display_name(changed_by),
                "rain_canceled_lesson_label": _availability_court_refund_lesson_label(availability),
            }
        )
        expense.note = _expense_build_note(
            meta.get("plain_note", ""),
            expense_type=meta.get("expense_type", EXPENSE_TYPE_COMMON),
            receipt_status=meta.get("receipt_status", EXPENSE_RECEIPT_NONE),
            receipt_check_status=meta.get("receipt_check_status", EXPENSE_RECEIPT_CHECK_UNCHECKED),
            approval_status=EXPENSE_APPROVAL_REFUND_PENDING,
            extra_meta=extra_meta,
        )
        expense.save(update_fields=["note"])
        changed_count += 1

    return changed_count


def _expense_meta_row(expense):
    meta = _expense_parse_note(getattr(expense, "note", ""))
    return {
        "expense": expense,
        "plain_note": meta["plain_note"],
        "expense_type": meta["expense_type"],
        "expense_type_label": EXPENSE_TYPE_LABELS.get(meta["expense_type"], meta["expense_type"]),
        "is_payout": meta["expense_type"] in EXPENSE_PAYOUT_TYPES,
        "meta": meta,
        "receipt_status": meta["receipt_status"],
        "receipt_status_label": _choice_label(EXPENSE_RECEIPT_CHOICES, meta["receipt_status"]),
        "receipt_check_status": meta["receipt_check_status"],
        "receipt_check_status_label": _choice_label(EXPENSE_RECEIPT_CHECK_CHOICES, meta["receipt_check_status"]),
        "approval_status": meta["approval_status"],
        "approval_status_label": _choice_label(EXPENSE_APPROVAL_CHOICES, meta["approval_status"]),
        "is_refund_pending": meta["approval_status"] == EXPENSE_APPROVAL_REFUND_PENDING,
        "is_refunded": meta["approval_status"] == EXPENSE_APPROVAL_REFUNDED,
        "court_refund_slot_key": meta.get("court_refund_slot_key", ""),
        "court_refund_lesson_label": meta.get("court_refund_lesson_label", ""),
        "court_refund_facility_label": meta.get("court_refund_facility_label", ""),
        "rain_canceled_lesson_label": meta.get("rain_canceled_lesson_label", ""),
    }


def _stringing_status_label(order):
    try:
        return order.get_status_display()
    except Exception:
        return str(getattr(order, "status", "") or "-")


def _stringing_delivery_label(order):
    if bool(getattr(order, "delivery_requested", False)):
        return "デリバリー"
    return "デリバリー無し"


def _stringing_status_key(order):
    return str(getattr(order, "status", "") or "")


def _safe_display_name_maybe(user):
    if not user:
        return "-"
    try:
        return user.display_name()
    except Exception:
        return str(user)


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

    coach_queryset = User.objects.filter(role__in=("coach", "contractor_coach")).order_by("full_name", "username", "id")
    is_admin_mode = bool(getattr(request.user, "is_superuser", False) or getattr(request.user, "is_staff", False))
    if is_admin_mode:
        selected_coach_id = (request.GET.get("coach_id") or "").strip()
        selected_coach = (
            coach_queryset.filter(pk=selected_coach_id).first() if selected_coach_id else coach_queryset.first()
        )
    else:
        selected_coach = request.user
        selected_coach_id = str(request.user.pk)

    month_start, next_month = _month_start_end(selected_year, selected_month)

    def _money(value):
        try:
            return int(value or 0)
        except Exception:
            return 0

    is_selected_contractor_coach = bool(
        selected_coach and getattr(selected_coach, "role", "") == "contractor_coach"
    )
    contractor_hourly_wage = _money(getattr(selected_coach, "contractor_hourly_wage", 0)) if selected_coach else 0
    contractor_work_minutes = 0
    contractor_work_slot_count = 0
    contractor_work_slot_keys = set()

    def _reservation_duration_minutes(reservation):
        try:
            return max(int((reservation.end_at - reservation.start_at).total_seconds() // 60), 0)
        except Exception:
            return 0

    def _add_contractor_work_slot(reservation):
        nonlocal contractor_work_minutes, contractor_work_slot_count
        if not is_selected_contractor_coach or not selected_coach:
            return
        if reservation.status != Reservation.STATUS_ACTIVE:
            return
        key = (
            str(reservation.lesson_type or ""),
            str(getattr(reservation, "court_id", "") or ""),
            _to_event_datetime_str(reservation.start_at) or "",
            _to_event_datetime_str(reservation.end_at) or "",
            str(selected_coach.pk),
        )
        if key in contractor_work_slot_keys:
            return
        contractor_work_slot_keys.add(key)
        contractor_work_slot_count += 1
        contractor_work_minutes += _reservation_duration_minutes(reservation)

    def _reservation_coaches_for_split(reservation):
        """
        方式A：売上金額ベース配分。
        1人コーチは100%、複数コーチの固定レッスンは人数で均等按分。
        代行コーチが入っている予約は、実施者である代行コーチへ100%帰属させます。
        """
        if not reservation:
            return []

        substitute = getattr(reservation, "substitute_coach", None)
        if substitute and getattr(substitute, "role", "") in ("coach", "contractor_coach"):
            return [substitute]

        fixed_lesson = getattr(reservation, "fixed_lesson", None)
        if fixed_lesson:
            try:
                coaches = [
                    coach
                    for coach in fixed_lesson.all_coaches()
                    if coach and getattr(coach, "role", "") in ("coach", "contractor_coach")
                ]
                if coaches:
                    return coaches
            except Exception:
                pass

        assigned = _assigned_coach_for_reservation(reservation)
        if assigned and getattr(assigned, "role", "") in ("coach", "contractor_coach"):
            return [assigned]

        return []

    def _selected_coach_share(reservation):
        coaches = _reservation_coaches_for_split(reservation)
        if not selected_coach or not coaches:
            return 0, 0
        if selected_coach.pk not in {coach.pk for coach in coaches}:
            return 0, len(coaches)
        return 1, len(coaches)

    def _split_amount_for_selected_coach(amount, reservation):
        numerator, denominator = _selected_coach_share(reservation)
        if numerator <= 0 or denominator <= 0:
            return 0
        return int(_money(amount) * numerator / denominator)

    monthly_expenses = list(
        CoachExpense.objects.filter(
            expense_date__gte=month_start,
            expense_date__lt=next_month,
        ).select_related("created_by").order_by("expense_date", "id")
    )
    expense_meta_rows = [_expense_meta_row(expense) for expense in monthly_expenses]

    reservation_qs = (
        Reservation.objects.filter(
            start_at__date__gte=month_start,
            start_at__date__lt=next_month,
        )
        .select_related("user", "coach", "substitute_coach", "court", "fixed_lesson")
        .prefetch_related("ticket_consumptions__purchase")
        .order_by("start_at", "id")
    )

    active_coach_ids = set()
    active_regular_coach_ids = set()
    active_contractor_coach_ids = set()
    for reservation in reservation_qs:
        if reservation.status != Reservation.STATUS_ACTIVE:
            continue
        for coach in _reservation_coaches_for_split(reservation):
            active_coach_ids.add(coach.pk)
            if getattr(coach, "role", "") == "contractor_coach":
                active_contractor_coach_ids.add(coach.pk)
            else:
                active_regular_coach_ids.add(coach.pk)

    if not active_coach_ids:
        active_coach_ids = set(coach_queryset.values_list("pk", flat=True))

    if not active_regular_coach_ids:
        active_regular_coach_ids = set(coach_queryset.filter(role="coach").values_list("pk", flat=True))

    active_coach_count = len(active_coach_ids)
    common_expense_participant_count = len(active_regular_coach_ids)

    total_tickets = 0
    ticket_lesson_amount = 0
    preopen_paid_amount = 0
    preopen_unpaid_amount = 0
    preopen_waived_amount = 0
    preopen_target_count = 0
    preopen_paid_count = 0
    preopen_unpaid_count = 0
    preopen_waived_count = 0

    breakdown_rows = []
    reservation_rows = []
    preopen_rows = []
    breakdown_map = {}

    if selected_coach:
        for reservation in reservation_qs:
            share_numerator, share_denominator = _selected_coach_share(reservation)
            if share_numerator <= 0 or share_denominator <= 0:
                continue

            _add_contractor_work_slot(reservation)

            active_consumptions = (
                reservation.ticket_consumptions.filter(refunded_at__isnull=True)
                .select_related("purchase")
                .order_by("created_at", "id")
            )

            row_breakdown_items = []
            row_breakdown_map = {}
            row_tickets = 0
            row_gross_amount = 0

            for consumption in active_consumptions:
                unit_price = _money(consumption.unit_price_snapshot)
                tickets_used = _money(consumption.tickets_used)

                row_breakdown_map.setdefault(unit_price, 0)
                row_breakdown_map[unit_price] += tickets_used

                row_tickets += tickets_used
                row_gross_amount += unit_price * tickets_used

            row_share_amount = _split_amount_for_selected_coach(row_gross_amount, reservation)
            if row_tickets > 0 and row_share_amount > 0:
                total_tickets += row_tickets
                ticket_lesson_amount += row_share_amount

                for unit_price, tickets in sorted(row_breakdown_map.items(), key=lambda x: x[0]):
                    split_amount = int((unit_price * tickets) * share_numerator / share_denominator)
                    breakdown_map.setdefault(unit_price, {"tickets": 0, "amount": 0})
                    breakdown_map[unit_price]["tickets"] += tickets
                    breakdown_map[unit_price]["amount"] += split_amount

                    row_breakdown_items.append(
                        {
                            "label": f"{unit_price}円券" if unit_price > 0 else "価格不明券",
                            "tickets": tickets,
                            "amount": split_amount,
                        }
                    )

                reservation_rows.append(
                    {
                        "reservation": reservation,
                        "tickets": row_tickets,
                        "gross_amount": row_gross_amount,
                        "amount": row_share_amount,
                        "split_denominator": share_denominator,
                        "is_split": share_denominator > 1,
                        "breakdown_items": row_breakdown_items,
                        "assigned_coach_name": reservation.assigned_coach_display(),
                        "normal_coach_name": reservation.normal_coach_display(),
                        "substitute_coach_name": _display_name(reservation.substitute_coach)
                        if reservation.substitute_coach
                        else "",
                        "has_substitute": reservation.has_substitute_coach(),
                        "revenue_kind": "チケット消化",
                    }
                )

            if (
                reservation.status == Reservation.STATUS_ACTIVE
                and _is_preopen_cash_regular_lesson(reservation.lesson_type, reservation.start_at)
                and reservation.is_payment_tracking_required()
            ):
                payment_amount = _money(reservation.payment_amount or PREOPEN_CASH_PRICE)
                split_payment_amount = _split_amount_for_selected_coach(payment_amount, reservation)
                if split_payment_amount <= 0:
                    continue

                preopen_target_count += 1
                if reservation.payment_status == Reservation.PAYMENT_STATUS_PAID:
                    preopen_paid_count += 1
                    preopen_paid_amount += split_payment_amount
                elif reservation.payment_status == Reservation.PAYMENT_STATUS_WAIVED:
                    preopen_waived_count += 1
                    preopen_waived_amount += split_payment_amount
                else:
                    preopen_unpaid_count += 1
                    preopen_unpaid_amount += split_payment_amount

                preopen_rows.append(
                    {
                        "reservation": reservation,
                        "amount": split_payment_amount,
                        "gross_amount": payment_amount,
                        "split_denominator": share_denominator,
                        "is_split": share_denominator > 1,
                        "payment_status": reservation.payment_status,
                        "payment_status_label": reservation.payment_status_badge_label(),
                        "is_paid": reservation.payment_status == Reservation.PAYMENT_STATUS_PAID,
                        "is_unpaid": reservation.payment_status == Reservation.PAYMENT_STATUS_UNPAID,
                        "is_waived": reservation.payment_status == Reservation.PAYMENT_STATUS_WAIVED,
                        "assigned_coach_name": reservation.assigned_coach_display(),
                        "normal_coach_name": reservation.normal_coach_display(),
                        "substitute_coach_name": _display_name(reservation.substitute_coach)
                        if reservation.substitute_coach
                        else "",
                        "has_substitute": reservation.has_substitute_coach(),
                    }
                )

        for unit_price, values in sorted(breakdown_map.items(), key=lambda x: x[0]):
            breakdown_rows.append(
                {
                    "label": f"{unit_price}円券" if unit_price > 0 else "価格不明券",
                    "tickets": values["tickets"],
                    "amount": values["amount"],
                }
            )

    lesson_total_amount = ticket_lesson_amount + preopen_paid_amount

    stringing_order_qs = StringingOrder.objects.filter(
        created_at__date__gte=month_start,
        created_at__date__lt=next_month,
    ).select_related("user", "assigned_coach").order_by("-created_at", "-id")

    assigned_stringing_rows = []
    assigned_stringing_amount = 0
    total_stringing_amount = 0
    stringing_status_totals = {}
    stringing_delivery_totals = {}
    unassigned_stringing_count = 0

    for order in stringing_order_qs:
        order_total = _money(order.total_price())
        status_key = _stringing_status_key(order).lower()

        if "cancel" not in status_key:
            total_stringing_amount += order_total

        assigned_coach_id = getattr(order, "assigned_coach_id", None)
        if not assigned_coach_id:
            unassigned_stringing_count += 1

        if not selected_coach or assigned_coach_id != selected_coach.pk:
            continue

        if "cancel" in status_key:
            continue

        assigned_stringing_amount += order_total

        status_label = _stringing_status_label(order)
        delivery_label = _stringing_delivery_label(order)

        stringing_status_totals.setdefault(status_label, {"count": 0, "amount": 0})
        stringing_status_totals[status_label]["count"] += 1
        stringing_status_totals[status_label]["amount"] += order_total

        stringing_delivery_totals.setdefault(delivery_label, {"count": 0, "amount": 0})
        stringing_delivery_totals[delivery_label]["count"] += 1
        stringing_delivery_totals[delivery_label]["amount"] += order_total

        preferred_completion_date = getattr(order, "preferred_completion_date", None)
        if preferred_completion_date:
            try:
                preferred_label = preferred_completion_date.strftime("%Y-%m-%d")
            except Exception:
                preferred_label = str(preferred_completion_date)
        else:
            preferred_label = "-"

        assigned_stringing_rows.append(
            {
                "order": order,
                "total_price": order_total,
                "status_label": status_label,
                "delivery_label": delivery_label,
                "preferred_label": preferred_label,
            }
        )

    stringing_status_rows = [
        {
            "label": label,
            "count": values["count"],
            "amount": values["amount"],
        }
        for label, values in sorted(stringing_status_totals.items(), key=lambda x: x[0])
    ]
    stringing_delivery_rows = [
        {
            "label": label,
            "count": values["count"],
            "amount": values["amount"],
        }
        for label, values in sorted(stringing_delivery_totals.items(), key=lambda x: x[0])
    ]

    approved_common_expense_rows = []
    approved_personal_expense_rows = []
    for row in expense_meta_rows:
        if row["is_payout"]:
            continue
        if row["approval_status"] != EXPENSE_APPROVAL_APPROVED:
            continue
        if row["expense_type"] == EXPENSE_TYPE_COMMON:
            approved_common_expense_rows.append(row)
        else:
            if selected_coach and getattr(row["expense"].created_by, "pk", None) == selected_coach.pk:
                approved_personal_expense_rows.append(row)

    approved_common_expense_total = sum(_money(row["expense"].amount) for row in approved_common_expense_rows)
    personal_reimbursement_amount = sum(_money(row["expense"].amount) for row in approved_personal_expense_rows)

    contractor_hourly_pay_amount = int(contractor_work_minutes * contractor_hourly_wage / 60) if is_selected_contractor_coach else 0
    contractor_work_hours_text = f"{contractor_work_minutes // 60}時間{contractor_work_minutes % 60:02d}分"

    monthly_contractor_expense_total = 0
    contractor_month_slot_keys = set()
    for reservation in reservation_qs:
        if reservation.status != Reservation.STATUS_ACTIVE:
            continue
        coaches = _reservation_coaches_for_split(reservation)
        for coach in coaches:
            if getattr(coach, "role", "") != "contractor_coach":
                continue
            slot_key = (
                str(reservation.lesson_type or ""),
                str(getattr(reservation, "court_id", "") or ""),
                _to_event_datetime_str(reservation.start_at) or "",
                _to_event_datetime_str(reservation.end_at) or "",
                str(coach.pk),
            )
            if slot_key in contractor_month_slot_keys:
                continue
            contractor_month_slot_keys.add(slot_key)
            try:
                minutes = max(int((reservation.end_at - reservation.start_at).total_seconds() // 60), 0)
            except Exception:
                minutes = 0
            hourly_wage = _money(getattr(coach, "contractor_hourly_wage", 0))
            monthly_contractor_expense_total += int(minutes * hourly_wage / 60)

    common_expense_base_total = approved_common_expense_total + monthly_contractor_expense_total
    if selected_coach and getattr(selected_coach, "role", "") == "contractor_coach":
        per_coach_common_expense = 0
    elif selected_coach and selected_coach.pk in active_regular_coach_ids and common_expense_participant_count > 0:
        per_coach_common_expense = int(common_expense_base_total / common_expense_participant_count)
    elif not selected_coach and common_expense_participant_count > 0:
        per_coach_common_expense = int(common_expense_base_total / common_expense_participant_count)
    else:
        per_coach_common_expense = 0

    if is_selected_contractor_coach:
        lesson_compensation_amount = contractor_hourly_pay_amount
    else:
        lesson_compensation_amount = lesson_total_amount

    settlement_before_common = lesson_compensation_amount + assigned_stringing_amount + personal_reimbursement_amount
    estimated_salary = settlement_before_common - per_coach_common_expense
    salary_before_common = settlement_before_common
    total_amount = settlement_before_common
    total_expense_amount = sum(_money(row["expense"].amount) for row in expense_meta_rows) + monthly_contractor_expense_total

    common_category_totals = {}
    for row in approved_common_expense_rows:
        label = row["expense"].get_category_display()
        common_category_totals.setdefault(label, 0)
        common_category_totals[label] += _money(row["expense"].amount)

    common_category_rows = [
        {"label": label, "amount": amount}
        for label, amount in sorted(common_category_totals.items(), key=lambda x: x[0])
    ]

    personal_category_totals = {}
    for row in approved_personal_expense_rows:
        label = row["expense"].get_category_display()
        personal_category_totals.setdefault(label, 0)
        personal_category_totals[label] += _money(row["expense"].amount)

    personal_category_rows = [
        {"label": label, "amount": amount}
        for label, amount in sorted(personal_category_totals.items(), key=lambda x: x[0])
    ]

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
            "is_staff_mode": is_admin_mode,
            "is_admin_mode": is_admin_mode,
            "breakdown_rows": breakdown_rows,
            "reservation_rows": reservation_rows,
            "preopen_rows": preopen_rows,
            "expense_rows": [row["expense"] for row in expense_meta_rows],
            "expense_meta_rows": expense_meta_rows,
            "approved_common_expense_rows": approved_common_expense_rows,
            "approved_personal_expense_rows": approved_personal_expense_rows,
            "common_category_rows": common_category_rows,
            "personal_category_rows": personal_category_rows,
            "stringing_rows": assigned_stringing_rows,
            "stringing_status_rows": stringing_status_rows,
            "stringing_delivery_rows": stringing_delivery_rows,
            "unassigned_stringing_count": unassigned_stringing_count,
            "total_tickets": total_tickets,
            "ticket_lesson_amount": ticket_lesson_amount,
            "preopen_paid_amount": preopen_paid_amount,
            "preopen_unpaid_amount": preopen_unpaid_amount,
            "preopen_waived_amount": preopen_waived_amount,
            "preopen_target_count": preopen_target_count,
            "preopen_paid_count": preopen_paid_count,
            "preopen_unpaid_count": preopen_unpaid_count,
            "preopen_waived_count": preopen_waived_count,
            "lesson_total_amount": lesson_total_amount,
            "lesson_compensation_amount": lesson_compensation_amount,
            "is_selected_contractor_coach": is_selected_contractor_coach,
            "contractor_hourly_wage": contractor_hourly_wage,
            "contractor_work_minutes": contractor_work_minutes,
            "contractor_work_hours_text": contractor_work_hours_text,
            "contractor_work_slot_count": contractor_work_slot_count,
            "contractor_hourly_pay_amount": contractor_hourly_pay_amount,
            "assigned_stringing_amount": assigned_stringing_amount,
            "total_stringing_amount": total_stringing_amount,
            "personal_reimbursement_amount": personal_reimbursement_amount,
            "salary_before_common": salary_before_common,
            "settlement_before_common": settlement_before_common,
            "total_amount": total_amount,
            "total_expense_amount": total_expense_amount,
            "approved_common_expense_total": approved_common_expense_total,
            "monthly_contractor_expense_total": monthly_contractor_expense_total,
            "common_expense_base_total": common_expense_base_total,
            "common_expense_participant_count": common_expense_participant_count,
            "per_coach_common_expense": per_coach_common_expense,
            "active_coach_count": active_coach_count,
            "estimated_salary": estimated_salary,
        },
    )

@login_required
@require_http_methods(["GET", "POST"])
def coach_admin_settlement(request):
    """
    admin専用の月次精算画面。
    新しいDBテーブルを増やさず、既存の CoachExpense に給与支払い履歴をメタ情報付きで保存します。
    created_by は「支払先コーチ」として扱います。
    """
    is_admin_mode = bool(getattr(request.user, "is_superuser", False) or getattr(request.user, "is_staff", False))
    if not is_admin_mode:
        return HttpResponse("Forbidden", status=403)

    User = get_user_model()
    today = timezone.localdate()

    try:
        selected_year = int(request.GET.get("year") or request.POST.get("year") or today.year)
    except Exception:
        selected_year = today.year

    try:
        selected_month = int(request.GET.get("month") or request.POST.get("month") or today.month)
    except Exception:
        selected_month = today.month

    if selected_month < 1 or selected_month > 12:
        selected_month = today.month

    month_start, next_month = _month_start_end(selected_year, selected_month)
    coach_queryset = User.objects.filter(role__in=("coach", "contractor_coach")).order_by("full_name", "username", "id")

    def _money(value):
        try:
            return int(value or 0)
        except Exception:
            return 0

    def _month_url(year_value, month_value):
        return f"{reverse('club:coach_admin_settlement')}?{urlencode({'year': year_value, 'month': month_value})}"

    def _payout_redirect():
        return _month_url(selected_year, selected_month)

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action == "create_payout":
            coach_id = (request.POST.get("coach_id") or "").strip()
            payout_type = (request.POST.get("payout_type") or "").strip()
            raw_amount = (request.POST.get("amount") or "").strip()
            raw_paid_date = (request.POST.get("paid_date") or "").strip()
            note = (request.POST.get("note") or "").strip()

            target_coach = coach_queryset.filter(pk=coach_id).first()
            if not target_coach:
                messages.error(request, "支払先コーチを選択してください。")
                return redirect(_payout_redirect())

            if payout_type not in EXPENSE_PAYOUT_TYPES:
                messages.error(request, "支払種別が不正です。")
                return redirect(_payout_redirect())

            try:
                amount_value = int(raw_amount or "0")
            except Exception:
                messages.error(request, "金額は整数で入力してください。")
                return redirect(_payout_redirect())

            if amount_value <= 0:
                messages.error(request, "金額は1円以上で入力してください。")
                return redirect(_payout_redirect())

            try:
                paid_date = date.fromisoformat(raw_paid_date) if raw_paid_date else timezone.localdate()
            except Exception:
                messages.error(request, "支払日の形式が正しくありません。")
                return redirect(_payout_redirect())

            try:
                CoachExpense.objects.create(
                    expense_date=paid_date,
                    category=CoachExpense.CATEGORY_OTHER,
                    amount=amount_value,
                    note=_expense_build_note(
                        note,
                        expense_type=payout_type,
                        receipt_status=EXPENSE_RECEIPT_NONE,
                        receipt_check_status=EXPENSE_RECEIPT_CHECK_CHECKED,
                        approval_status=EXPENSE_APPROVAL_APPROVED,
                        extra_meta={
                            "record_kind": "coach_payout",
                            "recorded_by_id": getattr(request.user, "pk", None),
                            "recorded_by_name": _display_name(request.user),
                        },
                    ),
                    created_by=target_coach,
                )
                messages.success(
                    request,
                    f"{_display_name(target_coach)}さんへの{EXPENSE_TYPE_LABELS.get(payout_type, payout_type)} {amount_value:,}円を記録しました。",
                )
            except Exception as e:
                messages.error(request, f"支払い履歴を保存できませんでした: {e}")

            return redirect(_payout_redirect())

    reservations = list(
        Reservation.objects.filter(
            start_at__date__gte=month_start,
            start_at__date__lt=next_month,
            status=Reservation.STATUS_ACTIVE,
        )
        .select_related("user", "coach", "substitute_coach", "court", "fixed_lesson")
        .prefetch_related("ticket_consumptions__purchase")
        .order_by("start_at", "id")
    )

    def _reservation_coaches_for_split(reservation):
        substitute = getattr(reservation, "substitute_coach", None)
        if substitute and getattr(substitute, "role", "") in ("coach", "contractor_coach"):
            return [substitute]

        fixed_lesson = getattr(reservation, "fixed_lesson", None)
        if fixed_lesson:
            try:
                coaches = [coach for coach in fixed_lesson.all_coaches() if coach and getattr(coach, "role", "") in ("coach", "contractor_coach")]
                if coaches:
                    return coaches
            except Exception:
                pass

        assigned = _assigned_coach_for_reservation(reservation)
        if assigned and getattr(assigned, "role", "") in ("coach", "contractor_coach"):
            return [assigned]
        return []

    def _reservation_duration_minutes(reservation):
        try:
            return max(int((reservation.end_at - reservation.start_at).total_seconds() // 60), 0)
        except Exception:
            return 0

    active_coach_ids = set()
    active_regular_coach_ids = set()
    active_contractor_coach_ids = set()
    for reservation in reservations:
        for coach in _reservation_coaches_for_split(reservation):
            active_coach_ids.add(coach.pk)
            if getattr(coach, "role", "") == "contractor_coach":
                active_contractor_coach_ids.add(coach.pk)
            else:
                active_regular_coach_ids.add(coach.pk)
    if not active_coach_ids:
        active_coach_ids = set(coach_queryset.values_list("pk", flat=True))
    if not active_regular_coach_ids:
        active_regular_coach_ids = set(coach_queryset.filter(role="coach").values_list("pk", flat=True))
    active_coach_count = len(active_coach_ids)
    common_expense_participant_count = len(active_regular_coach_ids)

    monthly_expenses = list(
        CoachExpense.objects.filter(
            expense_date__gte=month_start,
            expense_date__lt=next_month,
        ).select_related("created_by").order_by("expense_date", "id")
    )
    expense_meta_rows = [_expense_meta_row(expense) for expense in monthly_expenses]

    approved_common_expense_rows = [
        row for row in expense_meta_rows
        if not row["is_payout"]
        and row["approval_status"] == EXPENSE_APPROVAL_APPROVED
        and row["expense_type"] == EXPENSE_TYPE_COMMON
    ]
    approved_personal_expense_rows = [
        row for row in expense_meta_rows
        if not row["is_payout"]
        and row["approval_status"] == EXPENSE_APPROVAL_APPROVED
        and row["expense_type"] == EXPENSE_TYPE_PERSONAL
    ]
    submitted_personal_expense_rows = [
        row for row in expense_meta_rows
        if not row["is_payout"]
        and row["expense_type"] == EXPENSE_TYPE_PERSONAL
        and row["approval_status"] in (EXPENSE_APPROVAL_SUBMITTED, EXPENSE_APPROVAL_APPROVED)
    ]
    payout_rows = [row for row in expense_meta_rows if row["is_payout"]]

    approved_common_expense_total = sum(_money(row["expense"].amount) for row in approved_common_expense_rows)
    per_coach_common_expense = 0

    coach_map = {}
    for coach in coach_queryset:
        coach_map[coach.pk] = {
            "coach": coach,
            "coach_name": _display_name(coach),
            "ticket_amount": 0,
            "preopen_paid_amount": 0,
            "preopen_unpaid_amount": 0,
            "preopen_waived_amount": 0,
            "stringing_amount": 0,
            "is_contractor_coach": getattr(coach, "role", "") == "contractor_coach",
            "contractor_hourly_wage": _money(getattr(coach, "contractor_hourly_wage", 0)),
            "contractor_work_minutes": 0,
            "contractor_work_slot_count": 0,
            "_contractor_work_slot_keys": set(),
            "contractor_hourly_pay_amount": 0,
            "lesson_compensation_amount": 0,
            "personal_reimbursement_due": 0,
            "salary_paid": 0,
            "reimbursement_paid": 0,
            "common_expense_share": 0,
            "reservation_count": 0,
        }

    for reservation in reservations:
        coaches = _reservation_coaches_for_split(reservation)
        if not coaches:
            continue

        ticket_total = 0
        for consumption in reservation.ticket_consumptions.filter(refunded_at__isnull=True):
            ticket_total += _money(consumption.unit_price_snapshot) * _money(consumption.tickets_used)

        payment_amount = _money(getattr(reservation, "payment_amount", 0) or PREOPEN_CASH_PRICE)
        is_preopen = (
            reservation.lesson_type == Reservation.LESSON_GENERAL
            and is_preopen_cash_lesson_date(reservation.start_at)
            and reservation.is_payment_tracking_required()
        )

        denominator = max(len(coaches), 1)
        for coach in coaches:
            row = coach_map.get(coach.pk)
            if not row:
                continue
            row["reservation_count"] += 1
            if row.get("is_contractor_coach"):
                work_key = (
                    str(reservation.lesson_type or ""),
                    str(getattr(reservation, "court_id", "") or ""),
                    _to_event_datetime_str(reservation.start_at) or "",
                    _to_event_datetime_str(reservation.end_at) or "",
                    str(coach.pk),
                )
                if work_key not in row["_contractor_work_slot_keys"]:
                    row["_contractor_work_slot_keys"].add(work_key)
                    row["contractor_work_slot_count"] += 1
                    row["contractor_work_minutes"] += _reservation_duration_minutes(reservation)
            if ticket_total > 0:
                row["ticket_amount"] += int(ticket_total / denominator)
            if is_preopen:
                split_payment = int(payment_amount / denominator)
                if reservation.payment_status == Reservation.PAYMENT_STATUS_PAID:
                    row["preopen_paid_amount"] += split_payment
                elif reservation.payment_status == Reservation.PAYMENT_STATUS_WAIVED:
                    row["preopen_waived_amount"] += split_payment
                else:
                    row["preopen_unpaid_amount"] += split_payment

    stringing_orders = list(
        StringingOrder.objects.filter(
            created_at__date__gte=month_start,
            created_at__date__lt=next_month,
        ).select_related("assigned_coach", "user")
    )
    stringing_total = 0
    for order in stringing_orders:
        status_key = _stringing_status_key(order).lower()
        if "cancel" in status_key:
            continue
        amount = _money(order.total_price())
        stringing_total += amount
        if getattr(order, "assigned_coach_id", None) in coach_map:
            coach_map[order.assigned_coach_id]["stringing_amount"] += amount

    for row in approved_personal_expense_rows:
        coach_id = getattr(row["expense"].created_by, "pk", None)
        if coach_id in coach_map:
            coach_map[coach_id]["personal_reimbursement_due"] += _money(row["expense"].amount)

    for row in payout_rows:
        coach_id = getattr(row["expense"].created_by, "pk", None)
        if coach_id not in coach_map:
            continue
        amount = _money(row["expense"].amount)
        if row["expense_type"] == EXPENSE_TYPE_SALARY_PAYOUT:
            coach_map[coach_id]["salary_paid"] += amount
        elif row["expense_type"] == EXPENSE_TYPE_REIMBURSEMENT_PAYOUT:
            coach_map[coach_id]["reimbursement_paid"] += amount

    for row in coach_map.values():
        row["contractor_hourly_pay_amount"] = int(
            row["contractor_work_minutes"] * row["contractor_hourly_wage"] / 60
        ) if row.get("is_contractor_coach") else 0
        row["contractor_work_hours_text"] = f"{row['contractor_work_minutes'] // 60}時間{row['contractor_work_minutes'] % 60:02d}分"

    contractor_hourly_pay_total = sum(row["contractor_hourly_pay_amount"] for row in coach_map.values())
    common_expense_base_total = approved_common_expense_total + contractor_hourly_pay_total
    per_coach_common_expense = int(common_expense_base_total / common_expense_participant_count) if common_expense_participant_count > 0 else 0

    coach_rows = []
    for row in coach_map.values():
        if row.get("is_contractor_coach"):
            row["common_expense_share"] = 0
        elif getattr(row["coach"], "pk", None) in active_regular_coach_ids:
            row["common_expense_share"] = per_coach_common_expense
        else:
            row["common_expense_share"] = 0

        lesson_revenue_amount = row["ticket_amount"] + row["preopen_paid_amount"]
        if row.get("is_contractor_coach"):
            lesson_compensation_amount = row["contractor_hourly_pay_amount"]
        else:
            lesson_compensation_amount = lesson_revenue_amount
        row["lesson_compensation_amount"] = lesson_compensation_amount
        lesson_and_work_amount = lesson_compensation_amount + row["stringing_amount"]
        salary_due = lesson_and_work_amount - row["common_expense_share"]
        reimbursement_due = row["personal_reimbursement_due"]
        unpaid_salary = salary_due - row["salary_paid"]
        unpaid_reimbursement = reimbursement_due - row["reimbursement_paid"]
        total_unpaid = unpaid_salary + unpaid_reimbursement
        row.update(
            {
                "lesson_revenue_amount": lesson_revenue_amount,
                "lesson_and_work_amount": lesson_and_work_amount,
                "salary_due": salary_due,
                "reimbursement_due": reimbursement_due,
                "unpaid_salary": unpaid_salary,
                "unpaid_reimbursement": unpaid_reimbursement,
                "total_unpaid": total_unpaid,
                "total_paid": row["salary_paid"] + row["reimbursement_paid"],
            }
        )
        row.pop("_contractor_work_slot_keys", None)
        coach_rows.append(row)

    coach_rows = sorted(coach_rows, key=lambda row: row["coach_name"])

    preopen_paid_total = sum(row["preopen_paid_amount"] for row in coach_rows)
    preopen_unpaid_total = sum(row["preopen_unpaid_amount"] for row in coach_rows)
    ticket_amount_total = sum(row["ticket_amount"] for row in coach_rows)
    ticket_purchase_total = sum(
        _money(purchase.total_tickets) * _money(purchase.unit_price)
        for purchase in TicketPurchase.objects.filter(purchased_at__date__gte=month_start, purchased_at__date__lt=next_month)
    )
    salary_due_total = sum(row["salary_due"] for row in coach_rows)
    reimbursement_due_total = sum(row["reimbursement_due"] for row in coach_rows)
    salary_paid_total = sum(row["salary_paid"] for row in coach_rows)
    reimbursement_paid_total = sum(row["reimbursement_paid"] for row in coach_rows)
    unpaid_salary_total = sum(row["unpaid_salary"] for row in coach_rows)
    unpaid_reimbursement_total = sum(row["unpaid_reimbursement"] for row in coach_rows)

    cash_in_total = preopen_paid_total + ticket_purchase_total + stringing_total
    cash_out_total = salary_paid_total + reimbursement_paid_total + approved_common_expense_total
    company_balance = cash_in_total - cash_out_total

    pending_personal_reimbursement_total = sum(_money(row["expense"].amount) for row in submitted_personal_expense_rows)

    payout_history_rows = []
    for row in sorted(payout_rows, key=lambda item: (item["expense"].expense_date, item["expense"].id), reverse=True):
        payout_history_rows.append(
            {
                "expense": row["expense"],
                "coach_name": _display_name(row["expense"].created_by),
                "payout_type_label": row["expense_type_label"],
                "amount": _money(row["expense"].amount),
                "plain_note": row["plain_note"],
                "recorded_by_name": row.get("meta", {}).get("recorded_by_name", "-"),
            }
        )

    prev_year = selected_year
    prev_month = selected_month - 1
    if prev_month == 0:
        prev_month = 12
        prev_year -= 1

    next_year_value = selected_year
    next_month_value = selected_month + 1
    if next_month_value == 13:
        next_month_value = 1
        next_year_value += 1

    return render(
        request,
        "coach/admin_settlement.html",
        {
            "selected_year": selected_year,
            "selected_month": selected_month,
            "month_label": f"{selected_year}年{selected_month}月",
            "prev_url": _month_url(prev_year, prev_month),
            "next_url": _month_url(next_year_value, next_month_value),
            "coach_options": coach_queryset,
            "today_value": timezone.localdate().isoformat(),
            "payout_type_choices": [
                (EXPENSE_TYPE_SALARY_PAYOUT, "給与支払い"),
                (EXPENSE_TYPE_REIMBURSEMENT_PAYOUT, "本人立替精算支払い"),
            ],
            "coach_rows": coach_rows,
            "payout_history_rows": payout_history_rows,
            "approved_common_expense_rows": approved_common_expense_rows,
            "approved_personal_expense_rows": approved_personal_expense_rows,
            "submitted_personal_expense_rows": submitted_personal_expense_rows,
            "preopen_paid_total": preopen_paid_total,
            "preopen_unpaid_total": preopen_unpaid_total,
            "ticket_amount_total": ticket_amount_total,
            "ticket_purchase_total": ticket_purchase_total,
            "stringing_total": stringing_total,
            "cash_in_total": cash_in_total,
            "approved_common_expense_total": approved_common_expense_total,
            "contractor_hourly_pay_total": contractor_hourly_pay_total,
            "common_expense_base_total": common_expense_base_total,
            "common_expense_participant_count": common_expense_participant_count,
            "salary_due_total": salary_due_total,
            "reimbursement_due_total": reimbursement_due_total,
            "salary_paid_total": salary_paid_total,
            "reimbursement_paid_total": reimbursement_paid_total,
            "unpaid_salary_total": unpaid_salary_total,
            "unpaid_reimbursement_total": unpaid_reimbursement_total,
            "pending_personal_reimbursement_total": pending_personal_reimbursement_total,
            "cash_out_total": cash_out_total,
            "company_balance": company_balance,
            "active_coach_count": active_coach_count,
            "per_coach_common_expense": per_coach_common_expense,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def coach_expense_manage(request):
    if not (_is_coach_user(request.user) or _is_staff_like(request.user)):
        return HttpResponse("Forbidden", status=403)

    is_admin_mode = bool(getattr(request.user, "is_superuser", False) or getattr(request.user, "is_staff", False))
    visible_queryset = CoachExpense.objects.select_related("created_by").all().order_by("-expense_date", "-id")
    if not is_admin_mode:
        visible_queryset = visible_queryset.filter(created_by=request.user)

    today = timezone.localdate()
    court_refund_lesson_choices = _court_refund_lesson_choices_for_user(
        request.user,
        start_date=today - timedelta(days=60),
        end_date=today + timedelta(days=120),
    )
    court_refund_lesson_choice_map = {
        row["key"]: row for row in court_refund_lesson_choices
    }

    if request.method == "POST":
        action = (request.POST.get("action") or "create").strip()

        if action == "update_meta":
            if not is_admin_mode:
                messages.error(request, "更新権限がありません。")
                return redirect("club:coach_expense_manage")

            expense_id = (request.POST.get("expense_id") or "").strip()
            expense = CoachExpense.objects.filter(pk=expense_id).first()
            if not expense:
                messages.error(request, "対象の経費が見つかりません。")
                return redirect("club:coach_expense_manage")

            current_meta = _expense_parse_note(expense.note)
            expense_type = (request.POST.get("expense_type") or current_meta["expense_type"]).strip()
            receipt_status = (request.POST.get("receipt_status") or current_meta["receipt_status"]).strip()
            receipt_check_status = (request.POST.get("receipt_check_status") or current_meta["receipt_check_status"]).strip()
            approval_status = (request.POST.get("approval_status") or current_meta["approval_status"]).strip()
            plain_note = current_meta["plain_note"]

            valid_expense_types = {value for value, _label in EXPENSE_TYPE_CHOICES}
            valid_receipt_status = {value for value, _label in EXPENSE_RECEIPT_CHOICES}
            valid_receipt_check = {value for value, _label in EXPENSE_RECEIPT_CHECK_CHOICES}
            valid_approval_status = {value for value, _label in EXPENSE_APPROVAL_CHOICES}

            if expense_type not in valid_expense_types:
                messages.error(request, "経費種別が不正です。")
                return redirect("club:coach_expense_manage")
            if receipt_status not in valid_receipt_status:
                messages.error(request, "領収書有無が不正です。")
                return redirect("club:coach_expense_manage")
            if receipt_check_status not in valid_receipt_check:
                messages.error(request, "領収書確認状況が不正です。")
                return redirect("club:coach_expense_manage")
            if approval_status not in valid_approval_status:
                messages.error(request, "承認状態が不正です。")
                return redirect("club:coach_expense_manage")

            extra_meta = {
                key: value
                for key, value in current_meta.items()
                if key not in {
                    "expense_type",
                    "receipt_status",
                    "receipt_check_status",
                    "approval_status",
                    "plain_note",
                }
            }
            if approval_status == EXPENSE_APPROVAL_REFUNDED:
                extra_meta["refunded_at"] = timezone.now().isoformat()
                extra_meta["refunded_by_id"] = getattr(request.user, "pk", None)
                extra_meta["refunded_by_name"] = _display_name(request.user)

            expense.note = _expense_build_note(
                plain_note,
                expense_type=expense_type,
                receipt_status=receipt_status,
                receipt_check_status=receipt_check_status,
                approval_status=approval_status,
                extra_meta=extra_meta,
            )
            expense.save(update_fields=["note"])
            messages.success(request, "経費ステータスを更新しました。")
            return redirect("club:coach_expense_manage")

        raw_expense_date = (request.POST.get("expense_date") or "").strip()
        raw_category = (request.POST.get("category") or "").strip()
        raw_amount = (request.POST.get("amount") or "").strip()
        raw_note = (request.POST.get("note") or "").strip()
        raw_court_refund_slot_key = (request.POST.get("court_refund_slot_key") or "").strip()
        raw_expense_type = (request.POST.get("expense_type") or EXPENSE_TYPE_PERSONAL).strip()
        raw_receipt_status = (request.POST.get("receipt_status") or EXPENSE_RECEIPT_NONE).strip()

        if is_admin_mode:
            raw_receipt_check_status = (request.POST.get("receipt_check_status") or EXPENSE_RECEIPT_CHECK_UNCHECKED).strip()
            raw_approval_status = (request.POST.get("approval_status") or EXPENSE_APPROVAL_APPROVED).strip()
        else:
            raw_receipt_check_status = EXPENSE_RECEIPT_CHECK_UNCHECKED
            raw_approval_status = EXPENSE_APPROVAL_SUBMITTED

        valid_categories = {value for value, _label in CoachExpense.CATEGORY_CHOICES}
        valid_expense_types = {value for value, _label in EXPENSE_TYPE_CHOICES}
        valid_receipt_status = {value for value, _label in EXPENSE_RECEIPT_CHOICES}
        valid_receipt_check = {value for value, _label in EXPENSE_RECEIPT_CHECK_CHOICES}
        valid_approval_status = {value for value, _label in EXPENSE_APPROVAL_CHOICES}

        try:
            expense_date_value = date.fromisoformat(raw_expense_date) if raw_expense_date else timezone.localdate()
        except Exception:
            messages.error(request, "経費日付の形式が正しくありません。")
            return redirect("club:coach_expense_manage")

        try:
            amount_value = int(raw_amount or "0")
        except Exception:
            messages.error(request, "金額は整数で入力してください。")
            return redirect("club:coach_expense_manage")

        if raw_category not in valid_categories:
            messages.error(request, "経費カテゴリが不正です。")
            return redirect("club:coach_expense_manage")

        if raw_expense_type not in valid_expense_types:
            messages.error(request, "経費種別が不正です。")
            return redirect("club:coach_expense_manage")

        if raw_receipt_status not in valid_receipt_status:
            messages.error(request, "領収書有無が不正です。")
            return redirect("club:coach_expense_manage")

        if raw_receipt_check_status not in valid_receipt_check:
            messages.error(request, "領収書確認状況が不正です。")
            return redirect("club:coach_expense_manage")

        if raw_approval_status not in valid_approval_status:
            messages.error(request, "承認状態が不正です。")
            return redirect("club:coach_expense_manage")

        if amount_value < 0:
            messages.error(request, "金額は0円以上で入力してください。")
            return redirect("club:coach_expense_manage")

        extra_meta = {}
        if raw_category == CoachExpense.CATEGORY_COURT:
            selected_court_refund_lesson = court_refund_lesson_choice_map.get(raw_court_refund_slot_key)
            if not selected_court_refund_lesson:
                messages.error(request, "コート費用は対象レッスンを選択してください。")
                return redirect("club:coach_expense_manage")
            extra_meta.update(
                {
                    "court_refund_slot_key": selected_court_refund_lesson["key"],
                    "court_refund_lesson_label": selected_court_refund_lesson["label"],
                    "court_refund_facility_label": selected_court_refund_lesson["facility_label"],
                }
            )

        try:
            expense = CoachExpense(
                expense_date=expense_date_value,
                category=raw_category,
                amount=amount_value,
                note=_expense_build_note(
                    raw_note,
                    expense_type=raw_expense_type,
                    receipt_status=raw_receipt_status,
                    receipt_check_status=raw_receipt_check_status,
                    approval_status=raw_approval_status,
                    extra_meta=extra_meta,
                ),
                created_by=request.user,
            )
            expense.full_clean()
            expense.save()
            messages.success(request, "経費を登録しました。")
        except ValidationError as e:
            if hasattr(e, "messages"):
                for message_text in e.messages:
                    messages.error(request, message_text)
            else:
                messages.error(request, "経費を登録できませんでした。")
        except Exception as e:
            messages.error(request, f"経費を登録できませんでした: {e}")

        return redirect("club:coach_expense_manage")

    month_start = today.replace(day=1)
    if today.month == 12:
        next_month = date(today.year + 1, 1, 1)
    else:
        next_month = date(today.year, today.month + 1, 1)

    current_month_queryset = list(
        visible_queryset.filter(expense_date__gte=month_start, expense_date__lt=next_month)
    )
    current_month_meta_rows = [_expense_meta_row(expense) for expense in current_month_queryset]
    current_month_accounting_rows = [
        row for row in current_month_meta_rows
        if not _expense_is_refund_status(row["approval_status"])
    ]
    refund_pending_rows = [
        row for row in current_month_meta_rows
        if row["approval_status"] == EXPENSE_APPROVAL_REFUND_PENDING
    ]
    refunded_rows = [
        row for row in current_month_meta_rows
        if row["approval_status"] == EXPENSE_APPROVAL_REFUNDED
    ]
    current_month_total = sum(int(row["expense"].amount or 0) for row in current_month_accounting_rows)
    refund_pending_total = sum(int(row["expense"].amount or 0) for row in refund_pending_rows)
    refunded_total = sum(int(row["expense"].amount or 0) for row in refunded_rows)

    category_totals = {}
    expense_type_totals = {}
    approval_totals = {}
    for row in current_month_accounting_rows:
        category_label = row["expense"].get_category_display()
        category_totals.setdefault(category_label, 0)
        category_totals[category_label] += int(row["expense"].amount or 0)

        expense_type_totals.setdefault(row["expense_type_label"], 0)
        expense_type_totals[row["expense_type_label"]] += int(row["expense"].amount or 0)

        approval_totals.setdefault(row["approval_status_label"], 0)
        approval_totals[row["approval_status_label"]] += 1

    category_rows = [
        {"label": label, "amount": amount}
        for label, amount in sorted(category_totals.items(), key=lambda x: x[0])
    ]
    expense_type_rows = [
        {"label": label, "amount": amount}
        for label, amount in sorted(expense_type_totals.items(), key=lambda x: x[0])
    ]
    approval_rows = [
        {"label": label, "count": count}
        for label, count in sorted(approval_totals.items(), key=lambda x: x[0])
    ]

    recent_expenses = [_expense_meta_row(expense) for expense in list(visible_queryset[:30])]

    return render(
        request,
        "coach/expense_form.html",
        {
            "recent_expenses": recent_expenses,
            "expense_category_choices": CoachExpense.CATEGORY_CHOICES,
            "expense_type_choices": EXPENSE_TYPE_CHOICES,
            "expense_receipt_choices": EXPENSE_RECEIPT_CHOICES,
            "expense_receipt_check_choices": EXPENSE_RECEIPT_CHECK_CHOICES,
            "expense_approval_choices": EXPENSE_APPROVAL_CHOICES,
            "court_refund_lesson_choices": court_refund_lesson_choices,
            "current_month_total": current_month_total,
            "refund_pending_total": refund_pending_total,
            "refund_pending_count": len(refund_pending_rows),
            "refunded_total": refunded_total,
            "refunded_count": len(refunded_rows),
            "category_rows": category_rows,
            "expense_type_rows": expense_type_rows,
            "approval_rows": approval_rows,
            "today_value": today.isoformat(),
            "is_admin_mode": is_admin_mode,
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
        return redirect(_lesson_calendar_landing_url())

    form = AuthenticationForm(request, data=request.POST or None)

    if request.method == "POST":
        if form.is_valid():
            login(request, form.get_user())
            if _needs_profile_completion(request.user):
                return redirect("club:profile_complete")
            if _needs_schedule_survey(request.user):
                messages.info(request, "ログインありがとうございます。最初にアンケートへご回答ください。")
                return redirect("club:schedule_survey")
            return redirect(_lesson_calendar_landing_url())
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
        return redirect(_lesson_calendar_landing_url())

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
            return redirect(_lesson_calendar_landing_url())

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
        return redirect(_lesson_calendar_landing_url())

    form = LineProfileCompletionForm(request.POST or None, instance=request.user)

    if request.method == "POST":
        if form.is_valid():
            form.save()
            messages.success(request, "会員情報の登録が完了しました。")
            if _needs_schedule_survey(request.user):
                messages.info(request, "続けてアンケートへご回答ください。")
                return redirect("club:schedule_survey")
            return redirect(_lesson_calendar_landing_url())
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
                    "target_level_display": _lesson_level_label(obj) or obj.get_target_level_display(),
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
                    "detail_url": reverse("club:reservation_detail", kwargs={"pk": obj.pk}),
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

    regular_availability_id = (request.GET.get("availability_id") or "").strip()
    regular_fixed_lesson_id = (request.GET.get("fixed_lesson_id") or "").strip()
    regular_lesson_date = (request.GET.get("lesson_date") or "").strip()

    if request.method == "GET" and (regular_availability_id or (regular_fixed_lesson_id and regular_lesson_date)):
        source_year = (request.GET.get("year") or "").strip()
        source_month = (request.GET.get("month") or "").strip()
        back_params = {}
        if source_year and source_month:
            back_params = {"year": source_year, "month": source_month}

        if back_params:
            back_url = f"{reverse('club:lesson_calendar')}?{urlencode(back_params)}"
        else:
            back_url = reverse("club:lesson_calendar")

        def _slot_counts_for_lesson(*, coach, court, lesson_type, start_at, end_at):
            member_count = Reservation.objects.filter(
                coach=coach,
                court=court,
                lesson_type=lesson_type,
                start_at=start_at,
                end_at=end_at,
                status=Reservation.STATUS_ACTIVE,
            ).count()
            waitlist_count = LessonWaitlist.objects.filter(
                coach=coach,
                court=court,
                lesson_type=lesson_type,
                start_at=start_at,
                end_at=end_at,
                status=LessonWaitlist.STATUS_WAITING,
            ).count()
            user_waitlist = LessonWaitlist.objects.filter(
                user=request.user,
                coach=coach,
                court=court,
                lesson_type=lesson_type,
                start_at=start_at,
                end_at=end_at,
                status=LessonWaitlist.STATUS_WAITING,
            ).first()
            user_reserved = Reservation.objects.filter(
                user=request.user,
                coach=coach,
                court=court,
                lesson_type=lesson_type,
                start_at=start_at,
                end_at=end_at,
                status__in=[Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING],
            ).exists()
            return member_count, waitlist_count, user_waitlist, user_reserved

        selected_lesson = None

        try:
            if regular_availability_id:
                availability = get_object_or_404(
                    CoachAvailability.objects.select_related("coach", "substitute_coach", "court"),
                    pk=regular_availability_id,
                )
                start_at = timezone.localtime(availability.start_at) if timezone.is_aware(availability.start_at) else availability.start_at
                end_at = timezone.localtime(availability.end_at) if timezone.is_aware(availability.end_at) else availability.end_at
                coach_name = _display_name(availability.assigned_coach() if hasattr(availability, "assigned_coach") else (availability.substitute_coach or availability.coach))
                capacity = int(availability.effective_capacity() if hasattr(availability, "effective_capacity") else availability.capacity or 0)
                target_level_label = availability.get_target_level_display()
                lesson_type_label = availability.get_lesson_type_display()
                member_count, waitlist_count, user_waitlist, user_reserved = _slot_counts_for_lesson(
                    coach=availability.coach,
                    court=availability.court,
                    lesson_type=availability.lesson_type,
                    start_at=availability.start_at,
                    end_at=availability.end_at,
                )
                can_submit = (
                    availability.status == CoachAvailability.STATUS_OPEN
                    and availability.start_at >= timezone.now()
                    and availability.lesson_type in (Reservation.LESSON_GENERAL, Reservation.LESSON_EVENT)
                    and _can_user_take_lessons(request.user)
                    and member_count < max(capacity, 1)
                    and not user_reserved
                )
                can_join_waitlist = (
                    availability.status == CoachAvailability.STATUS_OPEN
                    and availability.start_at >= timezone.now()
                    and availability.lesson_type in (Reservation.LESSON_GENERAL, Reservation.LESSON_EVENT)
                    and _can_user_take_lessons(request.user)
                    and member_count >= max(capacity, 1)
                    and not user_reserved
                    and not user_waitlist
                )
                can_cancel_waitlist = bool(user_waitlist)

                selected_lesson = {
                    "mode": "availability",
                    "availability_id": str(availability.pk),
                    "fixed_lesson_id": "",
                    "lesson_date": "",
                    "title": "通常レッスン" if availability.lesson_type == Reservation.LESSON_GENERAL else lesson_type_label,
                    "date_label": start_at.strftime("%Y年%-m月%-d日") if hasattr(start_at, "strftime") else str(start_at),
                    "time_label": f"{start_at:%H:%M}〜{end_at:%H:%M}",
                    "coach_name": coach_name,
                    "court_name": str(availability.court) if availability.court else "未定",
                    "lesson_type_label": lesson_type_label,
                    "target_level_label": target_level_label,
                    "capacity": max(capacity, 1),
                    "member_count": member_count,
                    "waitlist_count": waitlist_count,
                    "ticket_label": _regular_lesson_payment_label(availability.lesson_type, availability.start_at),
                    "confirm_note": _regular_lesson_confirm_note(availability.lesson_type, availability.start_at),
                    "is_preopen_cash": _is_preopen_cash_regular_lesson(availability.lesson_type, availability.start_at),
                    "can_submit": can_submit,
                    "can_join_waitlist": can_join_waitlist,
                    "can_cancel_waitlist": can_cancel_waitlist,
                    "disabled_reason": "" if can_submit else ("すでに予約済みです。" if user_reserved else ("キャンセル待ち登録済みです。" if user_waitlist else ("満員です。キャンセル待ち登録できます。" if can_join_waitlist else "このレッスンは現在予約できません。"))),
                }
            else:
                fixed_lesson = get_object_or_404(
                    FixedLesson.objects.select_related("coach", "coach_2", "coach_3", "court"),
                    pk=regular_fixed_lesson_id,
                    is_active=True,
                )
                try:
                    target_date = date.fromisoformat(regular_lesson_date)
                except Exception:
                    raise ValidationError("予約対象日が正しくありません。")

                try:
                    start_at, end_at = fixed_lesson._build_datetimes_for_date(target_date)
                except Exception:
                    start_hour = int(getattr(fixed_lesson, "start_hour", 0) or 0)
                    start_at = datetime.combine(target_date, datetime.min.time()).replace(hour=start_hour, minute=0)
                    if timezone.is_naive(start_at):
                        start_at = timezone.make_aware(start_at)
                    end_at = start_at + timedelta(hours=_lesson_calendar_duration_hours(fixed_lesson))

                start_local = timezone.localtime(start_at) if timezone.is_aware(start_at) else start_at
                end_local = timezone.localtime(end_at) if timezone.is_aware(end_at) else end_at
                capacity = int(fixed_lesson.effective_capacity() if hasattr(fixed_lesson, "effective_capacity") else fixed_lesson.capacity or 0)
                court = fixed_lesson.court or Court.objects.filter(is_active=True).order_by("id").first()
                repeat_start = getattr(fixed_lesson, "start_date", None)
                is_after_repeat_start = not repeat_start or target_date >= repeat_start
                primary_coach = fixed_lesson.primary_coach() if hasattr(fixed_lesson, "primary_coach") else fixed_lesson.coach
                member_count, waitlist_count, user_waitlist, user_reserved = _slot_counts_for_lesson(
                    coach=primary_coach,
                    court=court,
                    lesson_type=fixed_lesson.lesson_type,
                    start_at=start_at,
                    end_at=end_at,
                ) if court else (0, 0, None, False)
                try:
                    fixed_member_count = fixed_lesson.members.count()
                except Exception:
                    fixed_member_count = 0
                member_count = max(int(member_count or 0), int(fixed_member_count or 0))
                can_submit = (
                    start_at >= timezone.now()
                    and is_after_repeat_start
                    and fixed_lesson.lesson_type in (Reservation.LESSON_GENERAL, Reservation.LESSON_EVENT)
                    and _can_user_take_lessons(request.user)
                    and court is not None
                    and member_count < max(capacity, 1)
                    and not user_reserved
                )
                can_join_waitlist = (
                    start_at >= timezone.now()
                    and is_after_repeat_start
                    and fixed_lesson.lesson_type in (Reservation.LESSON_GENERAL, Reservation.LESSON_EVENT)
                    and _can_user_take_lessons(request.user)
                    and court is not None
                    and member_count >= max(capacity, 1)
                    and not user_reserved
                    and not user_waitlist
                )
                can_cancel_waitlist = bool(user_waitlist)

                selected_lesson = {
                    "mode": "fixed_lesson",
                    "availability_id": "",
                    "fixed_lesson_id": str(fixed_lesson.pk),
                    "lesson_date": target_date.isoformat(),
                    "title": _lesson_calendar_title(fixed_lesson),
                    "date_label": start_local.strftime("%Y年%-m月%-d日") if hasattr(start_local, "strftime") else str(target_date),
                    "time_label": f"{start_local:%H:%M}〜{end_local:%H:%M}",
                    "coach_name": _fixed_lesson_coach_names(fixed_lesson),
                    "court_name": str(court) if court else "未定",
                    "lesson_type_label": fixed_lesson.get_lesson_type_display(),
                    "target_level_label": _lesson_level_label(fixed_lesson) or fixed_lesson.get_target_level_display(),
                    "capacity": max(capacity, 1),
                    "member_count": member_count,
                    "waitlist_count": waitlist_count,
                    "ticket_label": _regular_lesson_payment_label(fixed_lesson.lesson_type, start_at),
                    "confirm_note": _regular_lesson_confirm_note(fixed_lesson.lesson_type, start_at),
                    "is_preopen_cash": _is_preopen_cash_regular_lesson(fixed_lesson.lesson_type, start_at),
                    "can_submit": can_submit,
                    "can_join_waitlist": can_join_waitlist,
                    "can_cancel_waitlist": can_cancel_waitlist,
                    "disabled_reason": "" if can_submit else ("この固定レッスンはまだ開始前です。" if not is_after_repeat_start else ("すでに予約済みです。" if user_reserved else ("キャンセル待ち登録済みです。" if user_waitlist else ("満員です。キャンセル待ち登録できます。" if can_join_waitlist else "このレッスンは現在予約できません。")))),
                }

            if hasattr(request.user, "can_book_level") and selected_lesson and regular_fixed_lesson_id:
                fixed_lesson = FixedLesson.objects.filter(pk=regular_fixed_lesson_id).first()
                if fixed_lesson and not _user_can_book_lesson_levels(request.user, fixed_lesson):
                    selected_lesson["can_submit"] = False
                    selected_lesson["can_join_waitlist"] = False
                    selected_lesson["disabled_reason"] = "ご自身のレベルでは予約できません。"

        except ValidationError as e:
            messages.error(request, str(e))
            return redirect(back_url)

        return render(
            request,
            "reservations/regular_lesson_confirm.html",
            {
                "selected_lesson": selected_lesson,
                "back_url": back_url,
                "target_year": source_year,
                "target_month": source_month,
            },
        )

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
                _send_email_notification_safely(
                    reservation.coach,
                    "【Play Design Tennis】個別レッスン申請",
                    coach_message,
                )
                if getattr(reservation, "substitute_coach_id", None):
                    _send_email_notification_safely(
                        reservation.substitute_coach,
                        "【Play Design Tennis】個別レッスン申請",
                        coach_message,
                    )

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
    _sync_fixed_lessons()

    now = timezone.now()

    qs = (
        Reservation.objects.select_related("user", "coach", "substitute_coach", "court", "availability", "fixed_lesson")
        .prefetch_related("ticket_consumptions__purchase")
        .all()
    )

    waitlist_qs = (
        LessonWaitlist.objects.select_related("user", "coach", "substitute_coach", "court", "availability", "fixed_lesson")
        .all()
    )

    # _is_staff_like() は coach も True になるため、コーチ判定を先に行う。
    # 業務委託コーチは「担当コーチ」と「受講者」の両方になり得るため、
    # 自分が担当する予約に加えて、自分自身の受講予約・キャンセル待ちも表示します。
    if _is_coach_user(request.user):
        reservation_ids = []
        for reservation in qs:
            if (
                reservation.user_id == request.user.pk
                or reservation.coach_id == request.user.pk
                or getattr(reservation, "substitute_coach_id", None) == request.user.pk
            ):
                reservation_ids.append(reservation.pk)
        qs = qs.filter(pk__in=reservation_ids)

        waitlist_ids = []
        for waitlist in waitlist_qs:
            if (
                waitlist.user_id == request.user.pk
                or waitlist.coach_id == request.user.pk
                or getattr(waitlist, "substitute_coach_id", None) == request.user.pk
            ):
                waitlist_ids.append(waitlist.pk)
        waitlist_qs = waitlist_qs.filter(pk__in=waitlist_ids)
    elif _is_staff_like(request.user):
        pass
    else:
        qs = qs.filter(user=request.user)
        waitlist_qs = waitlist_qs.filter(user=request.user)

    qs = qs.order_by("start_at", "id")
    waitlist_qs = waitlist_qs.order_by("start_at", "created_at", "id")

    def _reservation_row(reservation):
        can_cancel, cancel_reason = _can_user_cancel_reservation(request.user, reservation)
        return {
            "reservation": reservation,
            "can_cancel": can_cancel,
            "cancel_reason": cancel_reason,
            "assigned_coach_name": reservation.assigned_coach_display(),
            "normal_coach_name": reservation.normal_coach_display(),
            "substitute_coach_name": _display_name(reservation.substitute_coach) if reservation.substitute_coach else "",
            "has_substitute": reservation.has_substitute_coach(),
            "is_future": reservation.start_at >= now,
            "is_canceled": reservation.status in (Reservation.STATUS_CANCELED, Reservation.STATUS_RAIN_CANCELED),
            "is_pending": reservation.status == Reservation.STATUS_PENDING,
            "is_active": reservation.status == Reservation.STATUS_ACTIVE,
        }

    future_reservation_rows = []
    past_reservation_rows = []
    canceled_reservation_rows = []

    for reservation in qs:
        row = _reservation_row(reservation)
        if row["is_canceled"]:
            canceled_reservation_rows.append(row)
        elif reservation.start_at >= now:
            future_reservation_rows.append(row)
        else:
            past_reservation_rows.append(row)

    waitlist_rows = []
    for waitlist in waitlist_qs:
        can_cancel_waitlist = (
            waitlist.status == LessonWaitlist.STATUS_WAITING
            and waitlist.start_at >= now
            and _user_can_manage_waitlist(request.user, waitlist)
        )

        active_count = _active_reservation_count_for_slot(
            coach=waitlist.coach,
            court=waitlist.court,
            lesson_type=waitlist.lesson_type,
            start_at=waitlist.start_at,
            end_at=waitlist.end_at,
        )
        capacity = _capacity_for_waitlist_slot(waitlist)
        can_promote = (
            waitlist.status == LessonWaitlist.STATUS_WAITING
            and waitlist.start_at >= now
            and active_count < capacity
            and _coach_can_manage_waitlist(request.user, waitlist)
        )

        waitlist_rows.append(
            {
                "waitlist": waitlist,
                "can_cancel": can_cancel_waitlist,
                "can_promote": can_promote,
                "active_count": active_count,
                "capacity": capacity,
                "remaining_count": max(capacity - active_count, 0),
                "assigned_coach_name": waitlist.assigned_coach_display(),
                "normal_coach_name": _display_name(waitlist.coach),
                "substitute_coach_name": _display_name(waitlist.substitute_coach) if waitlist.substitute_coach else "",
                "has_substitute": bool(waitlist.substitute_coach_id),
            }
        )

    waiting_waitlist_rows = [row for row in waitlist_rows if row["waitlist"].status == LessonWaitlist.STATUS_WAITING]
    processed_waitlist_rows = [row for row in waitlist_rows if row["waitlist"].status != LessonWaitlist.STATUS_WAITING]

    return render(
        request,
        "reservations/list.html",
        {
            "future_reservation_rows": future_reservation_rows,
            "past_reservation_rows": past_reservation_rows,
            "canceled_reservation_rows": canceled_reservation_rows,
            "waiting_waitlist_rows": waiting_waitlist_rows,
            "processed_waitlist_rows": processed_waitlist_rows,
            # 旧テンプレート互換用
            "reservation_rows": future_reservation_rows + past_reservation_rows + canceled_reservation_rows,
            "waitlist_rows": waitlist_rows,
        },
    )
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

    waitlist_notified = _notify_first_waitlist_user_if_slot_open(reservation)
    if waitlist_notified:
        messages.success(request, "予約をキャンセルしました。キャンセル待ちの先頭会員へ空き通知を送信しました。")
    else:
        messages.success(request, "予約をキャンセルしました。")
    return redirect("club:reservation_list")



@login_required
@require_POST
def lesson_waitlist_cancel(request, pk):
    waitlist = get_object_or_404(
        LessonWaitlist.objects.select_related("user", "coach", "substitute_coach", "court", "availability", "fixed_lesson"),
        pk=pk,
    )

    can_access = (
        _is_staff_like(request.user)
        or waitlist.user_id == request.user.pk
        or (_is_coach_user(request.user) and (waitlist.coach_id == request.user.pk or getattr(waitlist, "substitute_coach_id", None) == request.user.pk))
    )
    if not can_access:
        return HttpResponse("Forbidden", status=403)

    if waitlist.status != LessonWaitlist.STATUS_WAITING:
        messages.info(request, "このキャンセル待ちはすでに処理済みです。")
        return redirect("club:reservation_list")

    if waitlist.start_at < timezone.now() and not _is_staff_like(request.user):
        messages.error(request, "開始済み・終了済みのキャンセル待ちは取り消せません。")
        return redirect("club:reservation_list")

    if waitlist.user_id == request.user.pk:
        reason = "会員が予約確認画面からキャンセル"
    elif _is_coach_user(request.user):
        reason = "コーチが予約確認画面からキャンセル"
    else:
        reason = "管理者が予約確認画面からキャンセル"

    waitlist.cancel(reason=reason)
    messages.success(request, "キャンセル待ちを取り消しました。")
    return redirect("club:reservation_list")


@login_required

@login_required
@require_POST
def lesson_waitlist_promote(request, pk):
    waitlist = get_object_or_404(
        LessonWaitlist.objects.select_related("user", "coach", "substitute_coach", "court", "availability", "fixed_lesson"),
        pk=pk,
    )

    if not _coach_can_manage_waitlist(request.user, waitlist):
        return HttpResponse("Forbidden", status=403)

    redirect_to = (request.POST.get("next") or "").strip()
    if not redirect_to.startswith("/"):
        redirect_to = reverse("club:reservation_list")

    if waitlist.status != LessonWaitlist.STATUS_WAITING:
        messages.info(request, "このキャンセル待ちはすでに処理済みです。")
        return redirect(redirect_to)

    if waitlist.start_at < timezone.now():
        messages.error(request, "開始済み・終了済みのキャンセル待ちは繰り上げできません。")
        return redirect(redirect_to)

    active_count = _active_reservation_count_for_slot(
        coach=waitlist.coach,
        court=waitlist.court,
        lesson_type=waitlist.lesson_type,
        start_at=waitlist.start_at,
        end_at=waitlist.end_at,
    )
    capacity = _capacity_for_waitlist_slot(waitlist)
    if active_count >= capacity:
        messages.error(request, "このレッスンはまだ満員のため、繰り上げできません。")
        return redirect(redirect_to)

    existing_reservation = Reservation.objects.filter(
        user=waitlist.user,
        coach=waitlist.coach,
        court=waitlist.court,
        lesson_type=waitlist.lesson_type,
        start_at=waitlist.start_at,
        end_at=waitlist.end_at,
        status__in=[Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING],
    ).first()
    if existing_reservation:
        waitlist.mark_converted()
        messages.info(request, "対象会員はすでに予約済みです。キャンセル待ちを処理済みにしました。")
        return redirect("club:reservation_detail", pk=existing_reservation.pk)

    try:
        with transaction.atomic():
            availability = waitlist.availability or CoachAvailability.objects.filter(
                coach=waitlist.coach,
                court=waitlist.court,
                lesson_type=waitlist.lesson_type,
                start_at=waitlist.start_at,
                end_at=waitlist.end_at,
            ).first()

            reservation = Reservation(
                user=waitlist.user,
                coach=waitlist.coach,
                substitute_coach=waitlist.substitute_coach,
                court=waitlist.court,
                availability=availability,
                fixed_lesson=waitlist.fixed_lesson,
                lesson_type=waitlist.lesson_type,
                target_level=waitlist.target_level,
                target_level_2=getattr(waitlist, "target_level_2", "") or "",
                start_at=waitlist.start_at,
                end_at=waitlist.end_at,
                status=Reservation.STATUS_ACTIVE,
                custom_ticket_price=getattr(availability, "custom_ticket_price", 0) if availability else 0,
                custom_duration_hours=getattr(availability, "custom_duration_hours", 0) if availability else 0,
            )
            reservation.full_clean()
            reservation.save()
            reservation.consume_tickets(
                reason=TicketLedger.REASON_RESERVATION_USE,
                created_by=request.user,
                note=f"キャンセル待ち繰り上げ: {reservation.start_at:%Y-%m-%d %H:%M}",
            )
            waitlist.mark_converted()

        messages.success(request, f"{_display_name(reservation.user)} さんを予約へ繰り上げました。")
        return redirect("club:reservation_detail", pk=reservation.pk)

    except ValidationError as e:
        if hasattr(e, "messages"):
            for message_text in e.messages:
                messages.error(request, message_text)
        else:
            messages.error(request, str(e))
    except Exception as e:
        messages.error(request, f"キャンセル待ちの繰り上げに失敗しました: {e}")

    return redirect(redirect_to)

@require_http_methods(["GET", "POST"])
def coach_availability_list(request):
    _sync_fixed_lessons()

    if not (_is_coach_user(request.user) or _is_staff_like(request.user)):
        return HttpResponse("Forbidden", status=403)

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        if action == "rain_cancel_slot":
            availability_id = (request.POST.get("availability_id") or "").strip()
            availability = get_object_or_404(
                CoachAvailability.objects.select_related("coach", "substitute_coach", "court"),
                pk=availability_id,
            )

            if not _availability_can_manage(request.user, availability):
                return HttpResponse("Forbidden", status=403)

            active_reservations = _active_reservations_for_availability(availability)
            if not active_reservations:
                messages.warning(request, "この時間枠に雨天中止対象の予約はありません。")
                return redirect("club:coach_availability_list")

            canceled_count = 0
            for reservation in active_reservations:
                try:
                    with transaction.atomic():
                        succeeded = reservation.mark_rain_canceled(
                            created_by=request.user,
                            reason="雨天中止（スケジュール管理から実行）",
                        )
                    if succeeded:
                        canceled_count += 1
                        member_message = build_reservation_rain_canceled_message(reservation)
                        _send_line_notification_safely(reservation.user, member_message)
                except Exception:
                    continue

            if canceled_count > 0:
                refund_pending_count = _mark_court_expenses_refund_pending_for_rain_cancel(
                    availability,
                    changed_by=request.user,
                )
                message_text = (
                    f"雨天中止を実行しました。対象予約 {canceled_count} 件を中止し、会員へ通知しました。"
                )
                if refund_pending_count:
                    message_text += f" コート費用 {refund_pending_count} 件を「雨天返金待ち」に差し戻しました。"
                else:
                    message_text += (
                        " 紐づく承認済みコート費用は見つかりませんでした。"
                        " 経費登録時に、対象レッスン（施設名・日付・時間帯）を選択しているか確認してください。"
                    )
                messages.success(request, message_text)
            else:
                messages.warning(request, "雨天中止の対象予約はありませんでした。")

            return redirect("club:coach_availability_list")

        messages.error(request, "不正な操作です。")
        return redirect("club:coach_availability_list")

    qs = CoachAvailability.objects.select_related("coach", "substitute_coach", "court").all()

    if _is_coach_user(request.user):
        qs = qs.filter(coach=request.user)

    availabilities = list(qs.order_by("start_at"))

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
    else:
        pending_reservations = [
            reservation
            for reservation in pending_qs
            if reservation.coach_id == request.user.pk or getattr(reservation, "substitute_coach_id", None) == request.user.pk
        ]

    availability_rows = []
    for availability in availabilities:
        active_reservations = _active_reservations_for_availability(availability)
        waiting_count = LessonWaitlist.objects.filter(
            coach=availability.coach,
            court=availability.court,
            lesson_type=availability.lesson_type,
            start_at=availability.start_at,
            end_at=availability.end_at,
            status=LessonWaitlist.STATUS_WAITING,
        ).count()
        availability_rows.append(
            {
                "availability": availability,
                "active_reservation_count": len(active_reservations),
                "waitlist_count": waiting_count,
                "can_rain_cancel": len(active_reservations) > 0,
            }
        )

    return render(
        request,
        "coach/availability_list.html",
        {
            "availability_rows": availability_rows,
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
        _send_email_notification_safely(
            reservation.user,
            "【Play Design Tennis】個別レッスン申請 承認通知",
            member_message,
        )

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
        _send_email_notification_safely(
            reservation.user,
            "【Play Design Tennis】個別レッスン申請 却下通知",
            member_message,
        )

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

    raw_next = request.GET.get("next")
    if raw_next:
        next_url = _normalize_next_url(raw_next)
        if next_url in ("/", reverse("club:home")):
            next_url = _lesson_calendar_landing_url()
    else:
        next_url = _lesson_calendar_landing_url()

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
    next_url = _normalize_next_url(request.session.pop("line_login_next", _lesson_calendar_landing_url()))

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
            return redirect(_lesson_calendar_landing_url())

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

        if next_url in ("/", reverse("club:home")):
            next_url = _lesson_calendar_landing_url()
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
        "home_url": _lesson_calendar_landing_url(),
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
                    "redirectUrl": _lesson_calendar_landing_url(),
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
                "redirectUrl": _lesson_calendar_landing_url(),
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
        "line_login_url": f"{reverse('club:line_login_start')}?next={urllib.parse.quote(_lesson_calendar_landing_url())}",
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



def _shop_brand_label_map():
    brand_map = dict(ShopEstimateRequest.BRAND_CHOICES)
    brand_map.setdefault("solinco", "Solinco")
    brand_map.setdefault("luxilon", "Luxilon")
    return brand_map


def _shop_category_label_map():
    return dict(ShopEstimateRequest.CATEGORY_CHOICES)


def _shop_brand_catalog_links(brand_value, category_value):
    category_links = {
        ShopEstimateRequest.BRAND_YONEX: {
            ShopEstimateRequest.CATEGORY_RACKET: [
                {"label": "YONEX ラケット一覧", "url": "https://www.yonex.co.jp/tennis/racquets/"},
                {"label": "YONEX テニス TOP", "url": "https://www.yonex.co.jp/tennis/"},
            ],
            ShopEstimateRequest.CATEGORY_STRING: [
                {"label": "YONEX ストリング一覧", "url": "https://www.yonex.co.jp/tennis/strings/"},
                {"label": "YONEX テニス TOP", "url": "https://www.yonex.co.jp/tennis/"},
            ],
            ShopEstimateRequest.CATEGORY_ACCESSORY: [
                {"label": "YONEX アクセサリ一覧", "url": "https://www.yonex.co.jp/tennis/accessories/"},
                {"label": "YONEX テニス TOP", "url": "https://www.yonex.co.jp/tennis/"},
            ],
        },
        ShopEstimateRequest.BRAND_WILSON: {
            ShopEstimateRequest.CATEGORY_RACKET: [
                {"label": "Wilson ラケット一覧", "url": "https://jp.wilson.com/collections/tennis-rackets"},
                {"label": "Wilson Tennis TOP", "url": "https://jp.wilson.com/collections/tennis"},
            ],
            ShopEstimateRequest.CATEGORY_STRING: [
                {"label": "Wilson ストリング一覧", "url": "https://jp.wilson.com/collections/tennis-strings"},
                {"label": "Wilson Tennis TOP", "url": "https://jp.wilson.com/collections/tennis"},
            ],
            ShopEstimateRequest.CATEGORY_ACCESSORY: [
                {"label": "Wilson アクセサリ一覧", "url": "https://jp.wilson.com/collections/tennis-accessories"},
                {"label": "Wilson Tennis TOP", "url": "https://jp.wilson.com/collections/tennis"},
            ],
        },
        ShopEstimateRequest.BRAND_BABOLAT: {
            ShopEstimateRequest.CATEGORY_RACKET: [
                {"label": "Babolat ラケット一覧", "url": "https://www.babolat.com/jp/tennis/racquets.html"},
                {"label": "Babolat Tennis TOP", "url": "https://www.babolat.com/jp/tennis.html"},
            ],
            ShopEstimateRequest.CATEGORY_STRING: [
                {"label": "Babolat ストリング一覧", "url": "https://www.babolat.com/jp/tennis/strings.html"},
                {"label": "Babolat Tennis TOP", "url": "https://www.babolat.com/jp/tennis.html"},
            ],
            ShopEstimateRequest.CATEGORY_ACCESSORY: [
                {"label": "Babolat アクセサリ一覧", "url": "https://www.babolat.com/jp/tennis/accessories.html"},
                {"label": "Babolat Tennis TOP", "url": "https://www.babolat.com/jp/tennis.html"},
            ],
        },
        ShopEstimateRequest.BRAND_HEAD: {
            ShopEstimateRequest.CATEGORY_RACKET: [
                {"label": "HEAD ラケット一覧", "url": "https://www.head.com/ja_JP/tennis/racquets"},
                {"label": "HEAD Tennis TOP", "url": "https://www.head.com/ja_JP/tennis"},
            ],
            ShopEstimateRequest.CATEGORY_STRING: [
                {"label": "HEAD ストリング一覧", "url": "https://www.head.com/ja_JP/tennis/strings"},
                {"label": "HEAD Tennis TOP", "url": "https://www.head.com/ja_JP/tennis"},
            ],
            ShopEstimateRequest.CATEGORY_ACCESSORY: [
                {"label": "HEAD アクセサリ一覧", "url": "https://www.head.com/ja_JP/tennis/accessories"},
                {"label": "HEAD Tennis TOP", "url": "https://www.head.com/ja_JP/tennis"},
            ],
        },
        ShopEstimateRequest.BRAND_PRINCE: {
            ShopEstimateRequest.CATEGORY_RACKET: [
                {"label": "Prince ラケット一覧", "url": "https://prince.co.jp/tennis/rackets/"},
                {"label": "Prince Tennis TOP", "url": "https://prince.co.jp/tennis/"},
            ],
            ShopEstimateRequest.CATEGORY_STRING: [
                {"label": "Prince ストリング一覧", "url": "https://prince.co.jp/tennis/strings/"},
                {"label": "Prince Tennis TOP", "url": "https://prince.co.jp/tennis/"},
            ],
            ShopEstimateRequest.CATEGORY_ACCESSORY: [
                {"label": "Prince アクセサリ一覧", "url": "https://prince.co.jp/tennis/goods/"},
                {"label": "Prince Tennis TOP", "url": "https://prince.co.jp/tennis/"},
            ],
        },
        ShopEstimateRequest.BRAND_DUNLOP: {
            ShopEstimateRequest.CATEGORY_RACKET: [
                {"label": "DUNLOP ラケット一覧", "url": "https://sports.dunlop.co.jp/tennis/products/racket/"},
                {"label": "DUNLOP Tennis TOP", "url": "https://sports.dunlop.co.jp/tennis/"},
            ],
            ShopEstimateRequest.CATEGORY_STRING: [
                {"label": "DUNLOP ストリング一覧", "url": "https://sports.dunlop.co.jp/tennis/products/string/"},
                {"label": "DUNLOP Tennis TOP", "url": "https://sports.dunlop.co.jp/tennis/"},
            ],
            ShopEstimateRequest.CATEGORY_ACCESSORY: [
                {"label": "DUNLOP アクセサリ一覧", "url": "https://sports.dunlop.co.jp/tennis/products/accessory/"},
                {"label": "DUNLOP Tennis TOP", "url": "https://sports.dunlop.co.jp/tennis/"},
            ],
        },
        ShopEstimateRequest.BRAND_TECHNIFIBRE: {
            ShopEstimateRequest.CATEGORY_RACKET: [
                {"label": "Tecnifibre ラケット一覧", "url": "https://www.tecnifibre.com/en/c/tennis-racquets/"},
                {"label": "Tecnifibre Tennis TOP", "url": "https://www.tecnifibre.com/en/tennis/"},
            ],
            ShopEstimateRequest.CATEGORY_STRING: [
                {"label": "Tecnifibre ストリング一覧", "url": "https://www.tecnifibre.com/en/c/tennis-strings/"},
                {"label": "Tecnifibre Tennis TOP", "url": "https://www.tecnifibre.com/en/tennis/"},
            ],
            ShopEstimateRequest.CATEGORY_ACCESSORY: [
                {"label": "Tecnifibre アクセサリ一覧", "url": "https://www.tecnifibre.com/en/c/accessories/"},
                {"label": "Tecnifibre Tennis TOP", "url": "https://www.tecnifibre.com/en/tennis/"},
            ],
        },
        "solinco": {
            ShopEstimateRequest.CATEGORY_RACKET: [],
            ShopEstimateRequest.CATEGORY_STRING: [
                {"label": "Solinco ガット資料", "url": "https://www.kimony.com/file/solinco2026-15.pdf"},
                {"label": "Solinco MACH-10 資料", "url": "https://www.kimony.com/file/MACH-10.pdf"},
                {"label": "Solinco ガット資料2", "url": "https://www.kimony.com/file/solinco2026-16.pdf"},
            ],
            ShopEstimateRequest.CATEGORY_ACCESSORY: [],
        },
        "luxilon": {
            ShopEstimateRequest.CATEGORY_RACKET: [],
            ShopEstimateRequest.CATEGORY_STRING: [
                {"label": "Luxilon ストリング一覧", "url": "https://jp.wilson.com/collections/tennis-luxilon-strings"},
                {"label": "Wilson Tennis TOP", "url": "https://jp.wilson.com/collections/tennis"},
            ],
            ShopEstimateRequest.CATEGORY_ACCESSORY: [],
        },
        ShopEstimateRequest.BRAND_OTHER: {
            ShopEstimateRequest.CATEGORY_RACKET: [],
            ShopEstimateRequest.CATEGORY_STRING: [],
            ShopEstimateRequest.CATEGORY_ACCESSORY: [],
        },
    }
    return category_links.get(brand_value, {}).get(category_value, [])


def _shop_brand_search_links(brand_value, keyword, item_label="商品"):
    keyword = (keyword or "").strip()
    if not keyword:
        return []

    encoded = urllib.parse.quote(keyword)
    links = {
        ShopEstimateRequest.BRAND_YONEX: [
            {
                "label": f"YONEX 公式で{item_label}検索",
                "url": f"https://www.yonex.co.jp/search/?keyword={encoded}",
            }
        ],
        ShopEstimateRequest.BRAND_WILSON: [
            {
                "label": f"Wilson 公式で{item_label}検索",
                "url": f"https://jp.wilson.com/search?q={encoded}",
            }
        ],
        ShopEstimateRequest.BRAND_BABOLAT: [
            {
                "label": f"Babolat 公式で{item_label}検索",
                "url": f"https://www.babolat.com/jp/search?cgid=root&prefn1=country&prefv1=JP&q={encoded}",
            }
        ],
        ShopEstimateRequest.BRAND_HEAD: [
            {
                "label": f"HEAD 公式で{item_label}検索",
                "url": f"https://www.head.com/ja_JP/search/{encoded}",
            }
        ],
        ShopEstimateRequest.BRAND_PRINCE: [
            {
                "label": f"Prince 公式で{item_label}検索",
                "url": f"https://prince.co.jp/tennis/search/?q={encoded}",
            }
        ],
        ShopEstimateRequest.BRAND_DUNLOP: [
            {
                "label": f"DUNLOP 公式で{item_label}検索",
                "url": f"https://sports.dunlop.co.jp/tennis/search/?keyword={encoded}",
            }
        ],
        ShopEstimateRequest.BRAND_TECHNIFIBRE: [
            {
                "label": f"Tecnifibre 公式で{item_label}検索",
                "url": f"https://www.tecnifibre.com/en/search?text={encoded}",
            }
        ],
        "solinco": [
            {
                "label": f"Solinco 参照資料で{item_label}確認",
                "url": "https://www.kimony.com/file/solinco2026-15.pdf",
            },
            {
                "label": "Solinco MACH-10 資料",
                "url": "https://www.kimony.com/file/MACH-10.pdf",
            },
        ],
        "luxilon": [
            {
                "label": f"Luxilon 公式で{item_label}検索",
                "url": f"https://jp.wilson.com/search?q={encoded}",
            }
        ],
        ShopEstimateRequest.BRAND_OTHER: [],
    }
    return links.get(brand_value, [])


def _safe_int(value, default=0):
    try:
        return int(str(value).replace(',', '').strip() or default)
    except Exception:
        return default


def _shop_build_form_data_from_request_obj(obj):
    return {
        "product_category": getattr(obj, "product_category", ShopEstimateRequest.CATEGORY_RACKET),
        "brand": getattr(obj, "brand", ShopEstimateRequest.BRAND_YONEX),
        "main_keyword": getattr(obj, "main_keyword", "") or "",
        "main_product_name": getattr(obj, "main_product_name", "") or "",
        "main_official_price": str(getattr(obj, "main_official_price", "") or ""),
        "grip_size": str(getattr(obj, "grip_size", "") or ""),
        "string_source": getattr(obj, "string_source", ShopEstimateRequest.STRING_SOURCE_NONE),
        "string_keyword": getattr(obj, "string_keyword", "") or "",
        "string_product_name": getattr(obj, "string_product_name", "") or "",
        "string_official_price": str(getattr(obj, "string_official_price", "") or ""),
        "request_stringing": "1" if getattr(obj, "request_stringing", False) else "0",
        "request_delivery": "1" if getattr(obj, "request_delivery", False) else "0",
        "tension_lbs": str(getattr(obj, "tension_lbs", 50) or 50),
        "note": getattr(obj, "note", "") or "",
    }


def _shop_image_search_links(brand_value, keyword, item_label="商品画像"):
    keyword = (keyword or "").strip()
    brand_label = _shop_brand_label_map().get(brand_value, brand_value)
    if not keyword:
        return []

    official_like_query = f"{brand_label} {keyword} tennis"
    image_query = urllib.parse.quote(official_like_query)
    return [
        {
            "label": f"{brand_label} {item_label}確認",
            "url": f"https://www.google.com/search?tbm=isch&q={image_query}",
        }
    ]


def _shop_product_master_queryset():
    return (
        ShopProductMaster.objects.filter(is_active=True)
        .order_by("brand", "category", "product_type", "sort_order", "product_name", "id")
    )


def _shop_normalize_brand_value(raw_brand, text=""):
    brand = (raw_brand or "").strip().lower()
    source_text = (text or "").strip().lower()

    if brand == "solinco":
        return "solinco"
    if brand == "luxilon":
        return "luxilon"

    if brand in (
        ShopEstimateRequest.BRAND_YONEX,
        ShopEstimateRequest.BRAND_WILSON,
        ShopEstimateRequest.BRAND_BABOLAT,
        ShopEstimateRequest.BRAND_HEAD,
        ShopEstimateRequest.BRAND_PRINCE,
        ShopEstimateRequest.BRAND_DUNLOP,
        ShopEstimateRequest.BRAND_TECHNIFIBRE,
        ShopEstimateRequest.BRAND_OTHER,
    ):
        normalized = brand
    else:
        normalized = ShopEstimateRequest.BRAND_OTHER

    if normalized == ShopEstimateRequest.BRAND_OTHER:
        if "solinco" in source_text:
            return "solinco"
        if "luxilon" in source_text:
            return "luxilon"

    return normalized


def _shop_product_master_to_candidate_dict(obj):
    display_label = obj.display_name or obj.product_name
    keyword = obj.product_code or display_label
    source_text = " ".join(
        [
            str(display_label or ""),
            str(obj.product_name or ""),
            str(obj.product_code or ""),
            str(obj.description or ""),
        ]
    )
    return {
        "id": obj.pk,
        "product_type": obj.product_type,
        "category": obj.category,
        "brand": _shop_normalize_brand_value(obj.brand, source_text),
        "product_name": display_label,
        "keyword": keyword,
        "official_price": int(obj.official_price or 0),
        "image_url": obj.image_url or "",
        "product_url": obj.product_url or "",
        "note": obj.description or "",
        "product_code": obj.product_code or "",
        "spec_weight_unstrung": obj.spec_weight_unstrung or "",
        "spec_string_pattern": obj.spec_string_pattern or "",
        "spec_head_size": obj.spec_head_size or "",
        "spec_balance": obj.spec_balance or "",
        "spec_length": obj.spec_length or "",
        "spec_beam": obj.spec_beam or "",
        "spec_gauge": obj.spec_gauge or "",
        "spec_set_length": obj.spec_set_length or "",
        "spec_text": obj.spec_text(),
        "is_active": bool(obj.is_active),
    }


def _shop_master_candidate_lists(form_data):
    normalized_selected_brand = _shop_normalize_brand_value(form_data.get("brand", ""))

    all_candidates = [
        _shop_product_master_to_candidate_dict(obj)
        for obj in _shop_product_master_queryset()
    ]

    main_candidates = [
        item
        for item in all_candidates
        if item["product_type"] == ShopProductMaster.PRODUCT_TYPE_MAIN
        and item["brand"] == normalized_selected_brand
        and item["category"] == form_data["product_category"]
    ]

    string_candidates = [
        item
        for item in all_candidates
        if item["product_type"] == ShopProductMaster.PRODUCT_TYPE_STRING
        and item["brand"] == normalized_selected_brand
        and item["category"] == ShopProductMaster.CATEGORY_STRING
    ]

    return {
        "all_candidates": all_candidates,
        "main_candidates": main_candidates,
        "string_candidates": string_candidates,
    }


@login_required
@require_http_methods(["GET", "POST"])
def shop_estimate_view(request):

    profile_redirect = _require_profile_completed_for_booking(request)
    if profile_redirect:
        return profile_redirect

    survey_redirect = _require_schedule_survey(request)
    if survey_redirect:
        return survey_redirect

    brand_choices = list(ShopEstimateRequest.BRAND_CHOICES)
    if ("solinco", "Solinco") not in brand_choices:
        brand_choices.append(("solinco", "Solinco"))
    if ("luxilon", "Luxilon") not in brand_choices:
        brand_choices.append(("luxilon", "Luxilon"))
    category_choices = list(ShopEstimateRequest.CATEGORY_CHOICES)
    string_source_choices = list(ShopEstimateRequest.STRING_SOURCE_CHOICES)
    tension_choices = [(value, f"{value} lbs") for value in range(30, 61)]
    brand_label_map = _shop_brand_label_map()
    category_label_map = _shop_category_label_map()
    string_source_label_map = dict(string_source_choices)

    form_data = {
        "product_category": ShopEstimateRequest.CATEGORY_RACKET,
        "brand": ShopEstimateRequest.BRAND_YONEX,
        "main_keyword": "",
        "main_product_name": "",
        "main_official_price": "",
        "grip_size": "",
        "string_source": ShopEstimateRequest.STRING_SOURCE_NONE,
        "string_keyword": "",
        "string_product_name": "",
        "string_official_price": "",
        "request_stringing": "0",
        "request_delivery": "0",
        "tension_lbs": "50",
        "note": "",
    }
    estimate_result = None
    saved_request = None
    page_error = ""
    main_official_links = []
    main_catalog_links = []
    main_image_links = []
    string_official_links = []
    string_catalog_links = []
    string_image_links = []
    reused_request = None
    recent_requests = list(
        ShopEstimateRequest.objects.filter(user=request.user).order_by("-created_at", "-id")[:8]
    )
    master_candidate_context = _shop_master_candidate_lists(form_data)

    if request.method == "GET":
        reuse_id = (request.GET.get("reuse") or "").strip()
        if reuse_id.isdigit():
            reused_request = ShopEstimateRequest.objects.filter(user=request.user, pk=int(reuse_id)).first()
            if reused_request:
                form_data = _shop_build_form_data_from_request_obj(reused_request)
                form_data["brand"] = _shop_normalize_brand_value(
                    form_data["brand"],
                    " ".join([
                        form_data.get("main_product_name", ""),
                        form_data.get("main_keyword", ""),
                        form_data.get("string_product_name", ""),
                        form_data.get("string_keyword", ""),
                    ]),
                )
                messages.info(request, f"申込ID {reused_request.id} の内容をフォームへ再読み込みしました。")
                master_candidate_context = _shop_master_candidate_lists(form_data)

        main_official_links = _shop_brand_search_links(form_data["brand"], form_data["main_keyword"], item_label="商品")
        main_catalog_links = []
        main_image_links = _shop_image_search_links(form_data["brand"], form_data["main_keyword"], item_label="商品画像")
        if form_data["string_source"] == ShopEstimateRequest.STRING_SOURCE_OFFICIAL:
            string_official_links = _shop_brand_search_links(form_data["brand"], form_data["string_keyword"], item_label="ガット")
            string_catalog_links = []
            string_image_links = _shop_image_search_links(form_data["brand"], form_data["string_keyword"], item_label="ガット画像")

    if request.method == "POST":
        form_data = {
            "product_category": (request.POST.get("product_category") or ShopEstimateRequest.CATEGORY_RACKET).strip(),
            "brand": _shop_normalize_brand_value((request.POST.get("brand") or ShopEstimateRequest.BRAND_YONEX).strip()),
            "main_keyword": (request.POST.get("main_keyword") or "").strip(),
            "main_product_name": (request.POST.get("main_product_name") or "").strip(),
            "main_official_price": (request.POST.get("main_official_price") or "").strip(),
            "grip_size": (request.POST.get("grip_size") or "").strip(),
            "string_source": (request.POST.get("string_source") or ShopEstimateRequest.STRING_SOURCE_NONE).strip(),
            "string_keyword": (request.POST.get("string_keyword") or "").strip(),
            "string_product_name": (request.POST.get("string_product_name") or "").strip(),
            "string_official_price": (request.POST.get("string_official_price") or "").strip(),
            "request_stringing": "1" if (request.POST.get("request_stringing") or "") in ("1", "true", "on") else "0",
            "request_delivery": "1" if (request.POST.get("request_delivery") or "") in ("1", "true", "on") else "0",
            "tension_lbs": (request.POST.get("tension_lbs") or "50").strip(),
            "note": (request.POST.get("note") or "").strip(),
        }
        master_candidate_context = _shop_master_candidate_lists(form_data)

        main_official_price = _safe_int(form_data["main_official_price"], 0)
        string_official_price = _safe_int(form_data["string_official_price"], 0)
        request_stringing = form_data["request_stringing"] == "1"
        request_delivery = form_data["request_delivery"] == "1" if request_stringing else False
        tension_lbs = _safe_int(form_data["tension_lbs"], 50) if request_stringing else None

        main_official_links = _shop_brand_search_links(form_data["brand"], form_data["main_keyword"], item_label="商品")
        main_catalog_links = []
        main_image_links = _shop_image_search_links(form_data["brand"], form_data["main_keyword"], item_label="商品画像")
        if form_data["string_source"] == ShopEstimateRequest.STRING_SOURCE_OFFICIAL:
            string_official_links = _shop_brand_search_links(form_data["brand"], form_data["string_keyword"], item_label="ガット")
            string_catalog_links = []
            string_image_links = _shop_image_search_links(form_data["brand"], form_data["string_keyword"], item_label="ガット画像")

        valid_grip_sizes = {"", "1", "2", "3"}

        if main_official_price <= 0:
            page_error = "商品定価を入力してください。"
        elif form_data["grip_size"] not in valid_grip_sizes:
            page_error = "グリップサイズは1〜3で指定してください。"
        elif request_stringing and (tension_lbs is None or tension_lbs < 30 or tension_lbs > 60):
            page_error = "張り上げテンションは30〜60lbsで指定してください。"
        elif form_data["string_source"] == ShopEstimateRequest.STRING_SOURCE_OFFICIAL and string_official_price <= 0:
            page_error = "ガットも購入する場合は、ガット定価を入力してください。"
        else:
            main_sale_price = ShopEstimateRequest.sale_price_from_list_price(main_official_price)
            string_sale_price = (
                ShopEstimateRequest.sale_price_from_list_price(string_official_price)
                if form_data["string_source"] == ShopEstimateRequest.STRING_SOURCE_OFFICIAL
                else 0
            )
            stringing_fee = 1200 if request_stringing else 0
            delivery_fee = 500 if request_delivery else 0

            estimate_result = {
                "brand_label": brand_label_map.get(form_data["brand"], form_data["brand"]),
                "category_label": category_label_map.get(form_data["product_category"], form_data["product_category"]),
                "main_product_name": form_data["main_product_name"],
                "main_keyword": form_data["main_keyword"],
                "main_official_price": main_official_price,
                "main_sale_price": main_sale_price,
                "grip_size": form_data["grip_size"],
                "string_source": form_data["string_source"],
                "string_source_label": string_source_label_map.get(form_data["string_source"], form_data["string_source"]),
                "string_product_name": form_data["string_product_name"],
                "string_keyword": form_data["string_keyword"],
                "string_official_price": string_official_price,
                "string_sale_price": string_sale_price,
                "request_stringing": request_stringing,
                "request_delivery": request_delivery,
                "tension_lbs": tension_lbs,
                "stringing_fee": stringing_fee,
                "delivery_fee": delivery_fee,
                "estimated_total": main_sale_price + string_sale_price + stringing_fee + delivery_fee,
                "note": form_data["note"],
            }

            if (request.POST.get("action") or "") == "purchase":
                try:
                    create_kwargs = {
                        "user": request.user,
                        "product_category": form_data["product_category"],
                        "brand": form_data["brand"],
                        "main_keyword": form_data["main_keyword"],
                        "main_product_name": form_data["main_product_name"],
                        "main_official_price": main_official_price,
                        "string_source": form_data["string_source"],
                        "string_keyword": form_data["string_keyword"],
                        "string_product_name": form_data["string_product_name"],
                        "string_official_price": string_official_price,
                        "request_stringing": request_stringing,
                        "tension_lbs": tension_lbs,
                        "note": form_data["note"],
                    }

                    shop_estimate_request_field_names = {
                        field.name for field in ShopEstimateRequest._meta.get_fields()
                        if getattr(field, "concrete", False)
                    }
                    if "grip_size" in shop_estimate_request_field_names:
                        create_kwargs["grip_size"] = form_data["grip_size"]
                    if "request_delivery" in shop_estimate_request_field_names:
                        create_kwargs["request_delivery"] = request_delivery

                    saved_request = ShopEstimateRequest.objects.create(**create_kwargs)
                    messages.success(request, "物販の購入申込を受け付けました。")
                    return redirect("club:shop_estimate_complete", pk=saved_request.pk)
                except Exception as e:
                    page_error = f"購入申込の保存に失敗しました: {e}"

    return render(
        request,
        "shop/estimate.html",
        {
            "brand_choices": brand_choices,
            "category_choices": category_choices,
            "string_source_choices": string_source_choices,
            "tension_choices": tension_choices,
            "form_data": form_data,
            "estimate_result": estimate_result,
            "main_official_links": main_official_links,
            "main_catalog_links": main_catalog_links,
            "main_image_links": main_image_links,
            "string_official_links": string_official_links,
            "string_catalog_links": string_catalog_links,
            "string_image_links": string_image_links,
            "page_error": page_error,
            "saved_request": saved_request,
            "recent_requests": recent_requests,
            "reused_request": reused_request,
            "string_source_none": ShopEstimateRequest.STRING_SOURCE_NONE,
            "string_source_official": ShopEstimateRequest.STRING_SOURCE_OFFICIAL,
            "string_source_bring_in": ShopEstimateRequest.STRING_SOURCE_BRING_IN,
            "shop_candidate_support_message": "商品マスタから候補を表示しています。候補カードをクリックすると、商品名・定価・スペックが自動反映されます。",
            "shop_main_candidates": master_candidate_context.get("main_candidates", []),
            "shop_string_candidates": master_candidate_context.get("string_candidates", []),
            "shop_all_product_masters_json": master_candidate_context.get("all_candidates", []),
        },
    )


@login_required
@require_GET
def shop_estimate_history_view(request):
    profile_redirect = _require_profile_completed_for_booking(request)
    if profile_redirect:
        return profile_redirect

    survey_redirect = _require_schedule_survey(request)
    if survey_redirect:
        return survey_redirect

    estimate_requests = (
        ShopEstimateRequest.objects.filter(user=request.user)
        .order_by("-created_at", "-id")
    )

    history_rows = []
    for estimate_request in estimate_requests:
        main_sale_price = ShopEstimateRequest.sale_price_from_list_price(
            int(estimate_request.main_official_price or 0)
        )
        string_sale_price = 0
        if estimate_request.string_source == ShopEstimateRequest.STRING_SOURCE_OFFICIAL:
            string_sale_price = ShopEstimateRequest.sale_price_from_list_price(
                int(estimate_request.string_official_price or 0)
            )
        stringing_fee = 1200 if estimate_request.request_stringing else 0
        request_delivery = bool(getattr(estimate_request, "request_delivery", False))
        delivery_fee = 500 if request_delivery else 0
        estimated_total = main_sale_price + string_sale_price + stringing_fee + delivery_fee

        history_rows.append(
            {
                "estimate_request": estimate_request,
                "main_sale_price": main_sale_price,
                "string_sale_price": string_sale_price,
                "stringing_fee": stringing_fee,
                "delivery_fee": delivery_fee,
                "estimated_total": estimated_total,
            }
        )

    return render(
        request,
        "shop/history.html",
        {
            "history_rows": history_rows,
            "stringing_fee": 1200,
            "delivery_fee": 500,
            "string_source_official": ShopEstimateRequest.STRING_SOURCE_OFFICIAL,
            "string_source_bring_in": ShopEstimateRequest.STRING_SOURCE_BRING_IN,
            "string_source_none": ShopEstimateRequest.STRING_SOURCE_NONE,
        },
    )


@login_required
@require_GET
def shop_estimate_complete_view(request, pk):
    profile_redirect = _require_profile_completed_for_booking(request)
    if profile_redirect:
        return profile_redirect

    survey_redirect = _require_schedule_survey(request)
    if survey_redirect:
        return survey_redirect

    estimate_request = get_object_or_404(
        ShopEstimateRequest.objects.filter(user=request.user),
        pk=pk,
    )

    main_catalog_links = []
    main_official_links = _shop_brand_search_links(
        estimate_request.brand,
        estimate_request.main_keyword or estimate_request.main_product_name,
        item_label="商品",
    )
    main_image_links = _shop_image_search_links(
        estimate_request.brand,
        estimate_request.main_keyword or estimate_request.main_product_name,
        item_label="商品画像",
    )

    string_catalog_links = []
    string_official_links = []
    string_image_links = []
    if estimate_request.string_source == ShopEstimateRequest.STRING_SOURCE_OFFICIAL:
        string_catalog_links = []
        string_official_links = _shop_brand_search_links(
            estimate_request.brand,
            estimate_request.string_keyword or estimate_request.string_product_name,
            item_label="ガット",
        )
        string_image_links = _shop_image_search_links(
            estimate_request.brand,
            estimate_request.string_keyword or estimate_request.string_product_name,
            item_label="ガット画像",
        )

    return render(
        request,
        "shop/complete.html",
        {
            "estimate_request": estimate_request,
            "main_catalog_links": main_catalog_links,
            "main_official_links": main_official_links,
            "main_image_links": main_image_links,
            "string_catalog_links": string_catalog_links,
            "string_official_links": string_official_links,
            "string_image_links": string_image_links,
            "string_source_official": ShopEstimateRequest.STRING_SOURCE_OFFICIAL,
            "string_source_bring_in": ShopEstimateRequest.STRING_SOURCE_BRING_IN,
            "string_source_none": ShopEstimateRequest.STRING_SOURCE_NONE,
        },
    )

@login_required
@require_GET
def coach_revenue_summary(request):
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

    month_start, month_next = _month_start_end(selected_year, selected_month)

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

    coach_queryset = User.objects.filter(role__in=("coach", "contractor_coach")).order_by("full_name", "username", "id")
    is_admin_mode = bool(getattr(request.user, "is_superuser", False) or getattr(request.user, "is_staff", False))
    selected_coach_id = (request.GET.get("coach_id") or "").strip()

    selected_coach = None
    if selected_coach_id:
        selected_coach = coach_queryset.filter(pk=selected_coach_id).first()

    def _money(value):
        try:
            return int(value or 0)
        except Exception:
            return 0

    def _month_url(year_value, month_value):
        params = {"year": year_value, "month": month_value}
        if selected_coach_id:
            params["coach_id"] = selected_coach_id
        return f"{reverse('club:coach_revenue_summary')}?{urlencode(params)}"

    def _is_canceled_status(value):
        return "cancel" in str(value or "").lower() or str(value or "") in {"canceled", "rain_canceled"}

    reservations = list(
        Reservation.objects.filter(
            start_at__date__gte=month_start,
            start_at__date__lt=month_next,
            status=Reservation.STATUS_ACTIVE,
        )
        .select_related("user", "coach", "substitute_coach", "court", "availability", "fixed_lesson")
        .prefetch_related("ticket_consumptions__purchase")
        .order_by("start_at", "id")
    )

    preopen_rows = []
    ticket_lesson_rows = []
    coach_sales_map = {}

    preopen_reservation_count = 0
    preopen_sales_total = 0
    preopen_paid_total = 0
    preopen_unpaid_total = 0
    preopen_waived_total = 0
    preopen_unpaid_rows = []
    ticket_consumption_total = 0

    for reservation in reservations:
        assigned_coach = _assigned_coach_for_reservation(reservation)
        if selected_coach and getattr(assigned_coach, "pk", None) != selected_coach.pk:
            continue

        if (
            reservation.lesson_type == Reservation.LESSON_GENERAL
            and is_preopen_cash_lesson_date(reservation.start_at)
        ):
            amount = int(reservation.payment_amount or PREOPEN_CASH_PRICE)
            preopen_reservation_count += 1
            preopen_sales_total += amount

            if reservation.payment_status == Reservation.PAYMENT_STATUS_PAID:
                preopen_paid_total += amount
            elif reservation.payment_status == Reservation.PAYMENT_STATUS_WAIVED:
                preopen_waived_total += amount
            else:
                preopen_unpaid_total += amount

            row = {
                "reservation": reservation,
                "member_name": _display_name(reservation.user),
                "coach_name": _display_name(assigned_coach),
                "amount": amount,
                "payment_label": "7月プレオープン参加費",
                "payment_status_label": reservation.get_payment_status_display(),
            }
            preopen_rows.append(row)
            if reservation.payment_status == Reservation.PAYMENT_STATUS_UNPAID:
                preopen_unpaid_rows.append(row)

            coach_key = getattr(assigned_coach, "pk", None) or 0
            coach_sales_map.setdefault(
                coach_key,
                {
                    "coach_name": _display_name(assigned_coach),
                    "preopen_amount": 0,
                    "ticket_amount": 0,
                    "reservation_count": 0,
                },
            )
            coach_sales_map[coach_key]["preopen_amount"] += amount
            coach_sales_map[coach_key]["reservation_count"] += 1
            continue

        row_amount = 0
        breakdown_items = []
        active_consumptions = reservation.ticket_consumptions.filter(refunded_at__isnull=True).order_by("created_at", "id")
        for consumption in active_consumptions:
            unit_price = _money(consumption.unit_price_snapshot)
            tickets_used = _money(consumption.tickets_used)
            amount = unit_price * tickets_used
            row_amount += amount
            breakdown_items.append(
                {
                    "label": f"{unit_price}円券" if unit_price > 0 else "価格不明券",
                    "tickets": tickets_used,
                    "amount": amount,
                }
            )

        if row_amount <= 0:
            continue

        ticket_consumption_total += row_amount
        ticket_lesson_rows.append(
            {
                "reservation": reservation,
                "member_name": _display_name(reservation.user),
                "coach_name": _display_name(assigned_coach),
                "amount": row_amount,
                "breakdown_items": breakdown_items,
            }
        )

        coach_key = getattr(assigned_coach, "pk", None) or 0
        coach_sales_map.setdefault(
            coach_key,
            {
                "coach_name": _display_name(assigned_coach),
                "preopen_amount": 0,
                "ticket_amount": 0,
                "reservation_count": 0,
            },
        )
        coach_sales_map[coach_key]["ticket_amount"] += row_amount
        coach_sales_map[coach_key]["reservation_count"] += 1

    ticket_purchase_rows = []
    ticket_purchase_total = 0
    ticket_purchase_qs = (
        TicketPurchase.objects.filter(
            purchased_at__date__gte=month_start,
            purchased_at__date__lt=month_next,
        )
        .select_related("user", "created_by")
        .order_by("purchased_at", "id")
    )
    for purchase in ticket_purchase_qs:
        amount = _money(purchase.unit_price) * _money(purchase.total_tickets)
        ticket_purchase_total += amount
        ticket_purchase_rows.append(
            {
                "purchase": purchase,
                "member_name": _display_name(purchase.user),
                "label": purchase.label or purchase.get_purchase_type_display(),
                "tickets": _money(purchase.total_tickets),
                "unit_price": _money(purchase.unit_price),
                "amount": amount,
            }
        )

    stringing_rows = []
    stringing_total = 0
    stringing_qs = (
        StringingOrder.objects.filter(
            created_at__date__gte=month_start,
            created_at__date__lt=month_next,
        )
        .select_related("user", "assigned_coach")
        .order_by("created_at", "id")
    )
    for order in stringing_qs:
        if _is_canceled_status(getattr(order, "status", "")):
            continue
        amount = _money(order.total_price())
        stringing_total += amount
        stringing_rows.append(
            {
                "order": order,
                "member_name": _display_name(order.user),
                "coach_name": _display_name(order.assigned_coach),
                "status_label": order.get_status_display(),
                "amount": amount,
            }
        )

    shop_rows = []
    shop_reference_total = 0
    shop_qs = (
        ShopEstimateRequest.objects.filter(
            created_at__date__gte=month_start,
            created_at__date__lt=month_next,
        )
        .select_related("user")
        .order_by("created_at", "id")
    )
    for estimate in shop_qs:
        if _is_canceled_status(getattr(estimate, "handling_status", "")):
            continue
        amount = _money(getattr(estimate, "estimated_total", 0))
        shop_reference_total += amount
        shop_rows.append(
            {
                "estimate": estimate,
                "member_name": _display_name(estimate.user),
                "category": estimate.get_product_category_display(),
                "status_label": estimate.get_handling_status_display(),
                "amount": amount,
            }
        )

    expense_rows = []
    approved_expense_total = 0
    all_expense_total = 0
    expense_qs = CoachExpense.objects.filter(
        expense_date__gte=month_start,
        expense_date__lt=month_next,
    ).select_related("created_by").order_by("expense_date", "id")

    for expense in expense_qs:
        amount = _money(expense.amount)
        all_expense_total += amount
        meta_row = _expense_meta_row(expense)
        is_approved = meta_row.get("approval_status") == EXPENSE_APPROVAL_APPROVED
        if is_approved:
            approved_expense_total += amount

        expense_rows.append(
            {
                "expense": expense,
                "amount": amount,
                "category_label": expense.get_category_display(),
                "created_by_name": _display_name(expense.created_by),
                "plain_note": meta_row.get("plain_note", ""),
                "expense_type_label": meta_row.get("expense_type_label", "-"),
                "approval_status_label": meta_row.get("approval_status_label", "-"),
                "is_approved": is_approved,
            }
        )

    lesson_sales_total = preopen_sales_total + ticket_consumption_total
    operating_sales_total = lesson_sales_total + stringing_total
    reference_sales_total = operating_sales_total + shop_reference_total
    cash_basis_total = preopen_sales_total + ticket_purchase_total + stringing_total
    gross_profit_estimate = operating_sales_total - approved_expense_total
    reference_profit_estimate = reference_sales_total - approved_expense_total
    uncollected_preopen_estimate = preopen_unpaid_total

    coach_sales_rows = []
    for values in coach_sales_map.values():
        total_amount = _money(values.get("preopen_amount")) + _money(values.get("ticket_amount"))
        coach_sales_rows.append(
            {
                **values,
                "total_amount": total_amount,
            }
        )
    coach_sales_rows = sorted(coach_sales_rows, key=lambda row: (-row["total_amount"], row["coach_name"]))

    summary_cards = [
        {
            "label": "レッスン売上",
            "value": lesson_sales_total,
            "note": "7月プレオープン参加費 + チケット消化ベース",
        },
        {
            "label": "7月参加費 回収済み",
            "value": preopen_paid_total,
            "note": f"未回収 {preopen_unpaid_total}円 / 免除 {preopen_waived_total}円",
        },
        {
            "label": "チケット販売額",
            "value": ticket_purchase_total,
            "note": "購入時点の現金売上",
        },
        {
            "label": "ガット張り売上",
            "value": stringing_total,
            "note": "キャンセル以外",
        },
        {
            "label": "承認済み経費",
            "value": approved_expense_total,
            "note": "収支計算に反映",
        },
        {
            "label": "概算利益",
            "value": gross_profit_estimate,
            "note": "レッスン + ガット張り - 承認済み経費",
        },
        {
            "label": "物販参考売上",
            "value": shop_reference_total,
            "note": "見積・対応中を含む参考値",
        },
    ]

    return render(
        request,
        "coach/revenue_summary.html",
        {
            "selected_year": selected_year,
            "selected_month": selected_month,
            "month_label": f"{selected_year}年{selected_month}月",
            "prev_url": _month_url(prev_year, prev_month),
            "next_url": _month_url(next_year, next_month),
            "coach_options": coach_queryset,
            "selected_coach": selected_coach,
            "selected_coach_id": selected_coach_id,
            "is_admin_mode": is_admin_mode,
            "summary_cards": summary_cards,
            "preopen_reservation_count": preopen_reservation_count,
            "preopen_sales_total": preopen_sales_total,
            "preopen_paid_total": preopen_paid_total,
            "preopen_unpaid_total": preopen_unpaid_total,
            "preopen_waived_total": preopen_waived_total,
            "preopen_unpaid_rows": preopen_unpaid_rows,
            "uncollected_preopen_estimate": uncollected_preopen_estimate,
            "ticket_consumption_total": ticket_consumption_total,
            "ticket_purchase_total": ticket_purchase_total,
            "lesson_sales_total": lesson_sales_total,
            "stringing_total": stringing_total,
            "shop_reference_total": shop_reference_total,
            "operating_sales_total": operating_sales_total,
            "reference_sales_total": reference_sales_total,
            "cash_basis_total": cash_basis_total,
            "approved_expense_total": approved_expense_total,
            "all_expense_total": all_expense_total,
            "gross_profit_estimate": gross_profit_estimate,
            "reference_profit_estimate": reference_profit_estimate,
            "preopen_rows": preopen_rows,
            "ticket_lesson_rows": ticket_lesson_rows,
            "ticket_purchase_rows": ticket_purchase_rows,
            "stringing_rows": stringing_rows,
            "shop_rows": shop_rows,
            "expense_rows": expense_rows,
            "coach_sales_rows": coach_sales_rows,
            "preopen_cash_price": PREOPEN_CASH_PRICE,
        },
    )
