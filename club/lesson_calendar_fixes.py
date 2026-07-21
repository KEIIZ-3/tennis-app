from datetime import date

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

from .models import FixedLesson, LessonWaitlist, Reservation


def _local_date(value):
    if not value:
        return None
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return value.date()


def _fixed_lesson_member_count(fixed_lesson, lesson_date):
    fixed_member_ids = set(fixed_lesson.members.values_list("id", flat=True))
    count = len(fixed_member_ids)

    start_at, end_at = fixed_lesson._build_datetimes_for_date(lesson_date)
    reservations = (
        Reservation.objects.filter(
            fixed_lesson=fixed_lesson,
            start_at=start_at,
            end_at=end_at,
            status=Reservation.STATUS_ACTIVE,
        )
        .select_related("participant_snapshot")
        .order_by("id")
    )

    for reservation in reservations:
        if reservation.user_id not in fixed_member_ids:
            count += 1
            continue

        try:
            participant_type = reservation.participant_snapshot.participant_type
        except Exception:
            participant_type = "self"

        if participant_type != "self":
            count += 1

    return count


def _update_fixed_lesson_item(item):
    fixed_lesson_id = item.get("fixed_lesson_id")
    lesson_date_text = item.get("lesson_date")
    if not fixed_lesson_id or not lesson_date_text:
        return

    try:
        fixed_lesson = FixedLesson.objects.prefetch_related("members").get(pk=fixed_lesson_id)
        lesson_date = date.fromisoformat(lesson_date_text)
        member_count = _fixed_lesson_member_count(fixed_lesson, lesson_date)
    except Exception:
        return

    capacity = max(int(item.get("capacity") or 0), 1)
    previous_member_count = int(item.get("member_count") or 0)
    item["member_count"] = member_count
    item["remaining_count"] = max(capacity - member_count, 0)

    if member_count >= capacity and previous_member_count < capacity:
        if item.get("can_book"):
            item["can_book"] = False
            if item.get("is_waitlisted_by_user"):
                item["can_cancel_waitlist"] = True
                item["disabled_reason"] = "キャンセル待ち中です。"
            elif item.get("is_reserved_by_user"):
                item["disabled_reason"] = "予約済みです。"
            else:
                item["can_join_waitlist"] = True
                item["disabled_reason"] = "満員です。"


def _fix_calendar_context(context):
    if not isinstance(context, dict):
        return context

    seen = set()
    for item in context.get("schedule_rows", []):
        _update_fixed_lesson_item(item)
        seen.add(id(item))

    for week in context.get("calendar_weeks", []):
        for day in week:
            for item in day.get("items", []):
                if id(item) not in seen:
                    _update_fixed_lesson_item(item)

    return context


def _fixed_lesson_from_post(request):
    fixed_lesson_id = (request.POST.get("fixed_lesson_id") or "").strip()
    lesson_date_text = (request.POST.get("lesson_date") or "").strip()
    if not fixed_lesson_id or not lesson_date_text:
        return None, None

    try:
        fixed_lesson = FixedLesson.objects.prefetch_related("members").get(
            pk=fixed_lesson_id,
            is_active=True,
        )
        lesson_date = date.fromisoformat(lesson_date_text)
        return fixed_lesson, lesson_date
    except Exception:
        return None, None


def install_lesson_calendar_fix():
    from . import views

    original_view = getattr(views, "lesson_calendar_view", None)
    if not original_view or getattr(original_view, "_fixed_member_count_patch", False):
        return

    real_render = views.render

    def patched_view(request, *args, **kwargs):
        if request.method == "POST" and (request.POST.get("action") or "reserve").strip() == "reserve":
            fixed_lesson, lesson_date = _fixed_lesson_from_post(request)
            if fixed_lesson and lesson_date:
                try:
                    member_count = _fixed_lesson_member_count(fixed_lesson, lesson_date)
                    capacity = max(int(fixed_lesson.effective_capacity()), int(fixed_lesson.capacity or 0), 1)
                except Exception:
                    member_count = 0
                    capacity = 1

                if member_count >= capacity:
                    target_year = request.POST.get("year") or lesson_date.year
                    target_month = request.POST.get("month") or lesson_date.month
                    messages.error(request, "このレッスンは満員です。キャンセル待ちをご利用ください。")
                    return redirect(
                        f"{reverse('club:lesson_calendar')}?year={target_year}&month={target_month}"
                    )

        def patched_render(request_obj, template_name, context=None, *render_args, **render_kwargs):
            if template_name == "lesson_calendar.html":
                context = _fix_calendar_context(context)
            return real_render(
                request_obj,
                template_name,
                context,
                *render_args,
                **render_kwargs,
            )

        views.render = patched_render
        try:
            return original_view(request, *args, **kwargs)
        finally:
            views.render = real_render

    patched_view._fixed_member_count_patch = True
    patched_view.__name__ = original_view.__name__
    patched_view.__doc__ = original_view.__doc__
    views.lesson_calendar_view = patched_view


install_lesson_calendar_fix()
