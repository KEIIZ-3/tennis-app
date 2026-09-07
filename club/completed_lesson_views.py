import secrets
import unicodedata
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .completed_lesson_registration import (
    can_manage_completed_lessons,
    cancel_completed_lesson,
    register_completed_lesson,
)
from .models import CompletedLessonRegistration, Court, LessonTypeMixin, Reservation, User
from .forms import CoachAvailabilityForm
from .lesson_ticket_rules import standard_ticket_count
from .models import BUSINESS_END_HOUR, BUSINESS_START_HOUR


def _member_sort_key(member):
    # Userにはかな項目がないため、読みを推測せずcanonical表示名で安定ソートする。
    value = unicodedata.normalize("NFKC", member.display_name()).strip()
    hiragana = "".join(
        chr(ord(char) - 0x60) if "ァ" <= char <= "ヶ" else char
        for char in value
    )
    return (hiragana.casefold(), member.pk)


def _parse_datetime(date_text, time_text):
    value = datetime.strptime(f"{date_text} {time_text}", "%Y-%m-%d %H:%M")
    return timezone.make_aware(value, timezone.get_current_timezone())


@login_required
def register(request):
    if not can_manage_completed_lessons(request.user):
        return HttpResponseForbidden("Forbidden")
    members = list(User.objects.filter(
        role__in=User.LESSON_PARTICIPANT_ROLE_VALUES, is_active=True
    ).order_by("id"))
    members.sort(key=_member_sort_key)
    members_by_id = {str(member.pk): member for member in members}
    member_options = [
        {"id": member.pk, "label": member.display_name() if not member.display_name().startswith("line_") else "氏名未登録"}
        for member in members
    ]
    coaches = User.objects.filter(role__in=User.COACH_ROLE_VALUES, is_active=True).order_by("full_name", "username")
    if not (request.user.is_staff or request.user.is_superuser):
        coaches = coaches.filter(pk=request.user.pk)
    token = request.POST.get("idempotency_key") or secrets.token_urlsafe(24)
    selected_date = request.POST.get("date") or request.GET.get("date") or timezone.localdate().isoformat()
    start_time = request.POST.get("start_time") or "09:00"
    end_time = request.POST.get("end_time") or "11:00"
    mode = "completed"
    try:
        mode = "completed" if _parse_datetime(selected_date, end_time) <= timezone.now() else "scheduled"
    except (TypeError, ValueError):
        pass
    if request.method == "POST":
        try:
            posted_mode = request.POST.get("displayed_mode")
            if posted_mode not in ("completed", "scheduled") or posted_mode != mode:
                raise ValidationError("入力中に現在時刻をまたいだため、登録区分が変わりました。内容を確認してください。")
            if mode == "scheduled":
                future_data = request.POST.copy()
                future_data.update({
                    "start_date": selected_date,
                    "end_date": selected_date,
                    "start_hour": str(int(start_time.split(":", 1)[0])),
                    "end_hour": str(int(end_time.split(":", 1)[0])),
                    "target_level": User.LEVEL_ALL,
                    "target_level_2": "",
                    "coach_count": "1",
                    "court_count": "1",
                    "capacity": request.POST.get("capacity") or "1",
                    "custom_ticket_price": "0",
                    "custom_duration_hours": str(max(int((_parse_datetime(selected_date, end_time) - _parse_datetime(selected_date, start_time)).total_seconds() // 3600), 1)),
                })
                form = CoachAvailabilityForm(future_data, request_user=request.user)
                if not form.is_valid():
                    raise ValidationError(next(iter(form.errors.values()))[0])
                availability = form.save(commit=False)
                availability.save()
                messages.success(request, "実施予定レッスンを作成しました。")
                local_start = timezone.localtime(availability.start_at)
                return redirect(f"{reverse('club:lesson_calendar')}?year={local_start.year}&month={local_start.month}")
            count = int(request.POST.get("participant_count", "1"))
            if count < 1 or count > 10:
                raise ValidationError("顧客人数は1〜10名で指定してください。")
            participants = []
            for index in range(count):
                prefix = f"participant_{index}_"
                kind = request.POST.get(prefix + "kind")
                user = None
                if kind == "member":
                    user = members_by_id.get(request.POST.get(prefix + "user"))
                    if user is None:
                        raise ValidationError("参加可能な会員を選択してください。")
                participants.append({
                    "user": user,
                    "guest_name": request.POST.get(prefix + "guest_name", ""),
                    "payment_method": request.POST.get(prefix + "payment_method"),
                    "value": request.POST.get(prefix + "value"),
                })
            coach = coaches.get(pk=request.POST.get("coach"))
            court_payer = coaches.get(pk=request.POST.get("court_payer"))
            registration, created = register_completed_lesson(
                actor=request.user,
                start_at=_parse_datetime(request.POST.get("date"), request.POST.get("start_time")),
                end_at=_parse_datetime(request.POST.get("date"), request.POST.get("end_time")),
                lesson_type=request.POST.get("lesson_type"), coach=coach,
                court=Court.objects.get(pk=request.POST.get("court")), participants=participants,
                court_cost=request.POST.get("court_cost", 0), court_payer=court_payer,
                note=request.POST.get("note", ""), idempotency_key=token,
            )
            messages.success(request, "実施済みレッスンを登録しました。" if created else "この内容は登録済みです。")
            return redirect(f"/lesson-calendar/members/?availability_id={registration.availability_id}")
        except (ValidationError, ValueError, TypeError, User.DoesNotExist, Court.DoesNotExist) as exc:
            messages.error(request, exc.messages[0] if getattr(exc, "messages", None) else "入力内容を確認してください。")
    return render(request, "coach/completed_lesson_register.html", {
        "members": members, "member_options": member_options, "coaches": coaches, "courts": Court.objects.all(),
        "lesson_types": LessonTypeMixin.LESSON_TYPE_CHOICES,
        "participant_range": range(10), "idempotency_key": token,
        "selected_date": selected_date, "start_time": start_time, "end_time": end_time,
        "mode": mode,
        "business_start_hour": BUSINESS_START_HOUR,
        "business_end_hour": BUSINESS_END_HOUR,
        "ticket_defaults": {
            lesson_type: {
                str(hours): {
                    str(participants): standard_ticket_count(
                        lesson_type=lesson_type,
                        duration_hours=hours,
                        participant_count=participants,
                    )
                    for participants in range(1, 11)
                }
                for hours in range(1, BUSINESS_END_HOUR - BUSINESS_START_HOUR + 1)
            }
            for lesson_type, _label in LessonTypeMixin.LESSON_TYPE_CHOICES
            if lesson_type != Reservation.LESSON_EVENT
        },
    })


@login_required
def cancel(request, pk):
    registration = get_object_or_404(
        CompletedLessonRegistration.objects.select_related("availability__coach"), pk=pk
    )
    if not can_manage_completed_lessons(request.user, registration.availability.coach):
        return HttpResponseForbidden("Forbidden")
    if request.method != "POST":
        return redirect(f"/lesson-calendar/members/?availability_id={registration.availability_id}")
    try:
        _registration, changed = cancel_completed_lesson(registration_id=pk, actor=request.user)
        messages.success(request, "実施済み登録を取り消しました。チケット・売上・コート代・精算を元に戻しました。" if changed else "この実施済み登録はすでに取消済みです。")
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    local_start = timezone.localtime(registration.availability.start_at)
    return redirect(f"{reverse('club:lesson_calendar')}?year={local_start.year}&month={local_start.month}")
