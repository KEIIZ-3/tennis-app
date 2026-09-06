import secrets
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .completed_lesson_registration import (
    can_manage_completed_lessons,
    cancel_completed_lesson,
    register_completed_lesson,
)
from .models import CompletedLessonRegistration, Court, LessonTypeMixin, Reservation, User


def _parse_datetime(date_text, time_text):
    value = datetime.strptime(f"{date_text} {time_text}", "%Y-%m-%d %H:%M")
    return timezone.make_aware(value, timezone.get_current_timezone())


@login_required
def register(request):
    if not can_manage_completed_lessons(request.user):
        return HttpResponseForbidden("Forbidden")
    members = User.objects.filter(role__in=User.LESSON_PARTICIPANT_ROLE_VALUES, is_active=True).order_by("full_name", "username")
    coaches = User.objects.filter(role__in=User.COACH_ROLE_VALUES, is_active=True).order_by("full_name", "username")
    if not (request.user.is_staff or request.user.is_superuser):
        coaches = coaches.filter(pk=request.user.pk)
    token = request.POST.get("idempotency_key") or secrets.token_urlsafe(24)
    if request.method == "POST":
        try:
            count = int(request.POST.get("participant_count", "1"))
            if count < 1 or count > 10:
                raise ValidationError("顧客人数は1〜10名で指定してください。")
            participants = []
            for index in range(count):
                prefix = f"participant_{index}_"
                kind = request.POST.get(prefix + "kind")
                user = None
                if kind == "member":
                    user = members.get(pk=request.POST.get(prefix + "user"))
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
        "members": members, "coaches": coaches, "courts": Court.objects.all(),
        "lesson_types": LessonTypeMixin.LESSON_TYPE_CHOICES,
        "participant_range": range(10), "idempotency_key": token,
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
        messages.success(request, "実施済み登録を取り消しました。" if changed else "この実施済み登録はすでに取消済みです。")
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    return redirect("club:lesson_execution_manage")
