from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .lesson_participants import reservations_for_lesson
from .models import CoachAvailability, FixedLesson, Reservation, TicketPurchaseReservation
from .ticket_purchase_reservation_service import (
    approve_purchase_reservation,
    cancel_purchase_reservation,
    purchase_reversal_availability,
    REVERSAL_REASON_CHOICES,
    reverse_purchase_reservation,
    create_purchase_reservation,
    is_main_coach,
    pending_purchase_reservations_for_participants,
)


@login_required
@require_POST
def create(request):
    if request.user.role != request.user.ROLE_MEMBER:
        raise PermissionDenied
    try:
        create_purchase_reservation(user=request.user, product_code=request.POST.get("product"))
        messages.success(request, "チケット購入を予約しました。レッスン当日に現金でお支払いください。")
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    return redirect("club:tickets")


@login_required
@require_POST
def cancel(request, pk):
    try:
        cancel_purchase_reservation(reservation_id=pk, user=request.user)
        messages.success(request, "チケット購入予約をキャンセルしました。")
    except TicketPurchaseReservation.DoesNotExist:
        raise PermissionDenied
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    return redirect("club:tickets")


def _lesson_reservations(request):
    availability_id = request.GET.get("availability_id")
    fixed_lesson_id = request.GET.get("fixed_lesson_id")
    lesson_date = request.GET.get("lesson_date")
    if availability_id:
        availability = get_object_or_404(CoachAvailability, pk=availability_id)
        fixed_lesson = get_object_or_404(FixedLesson, pk=fixed_lesson_id) if fixed_lesson_id else None
        return reservations_for_lesson(
            availability=availability, fixed_lesson=fixed_lesson, lesson_type=availability.lesson_type,
            start_at=availability.start_at, end_at=availability.end_at,
            statuses=(Reservation.STATUS_ACTIVE,),
        )
    fixed_lesson = get_object_or_404(FixedLesson, pk=fixed_lesson_id)
    target_date = date.fromisoformat(lesson_date)
    start_at, end_at = fixed_lesson._build_datetimes_for_date(target_date)
    return reservations_for_lesson(
        fixed_lesson=fixed_lesson, coach=fixed_lesson.primary_coach(), court=fixed_lesson.court,
        lesson_type=fixed_lesson.lesson_type, start_at=start_at, end_at=end_at,
        statuses=(Reservation.STATUS_ACTIVE,),
    )


@login_required
@require_GET
def confirm(request):
    if not is_main_coach(request.user):
        raise PermissionDenied
    pending = pending_purchase_reservations_for_participants(_lesson_reservations(request))
    return render(request, "coach/ticket_purchase_confirm.html", {"purchase_reservations": pending})


@login_required
@require_POST
def approve(request, pk):
    if not is_main_coach(request.user):
        raise PermissionDenied
    lesson_reservations = _lesson_reservations(request)
    allowed_ids = set(lesson_reservations.exclude(user_id=None).values_list("user_id", flat=True))
    purchase_reservation = get_object_or_404(TicketPurchaseReservation, pk=pk)
    if purchase_reservation.user_id not in allowed_ids:
        raise PermissionDenied
    try:
        source_reservation = lesson_reservations.filter(user_id=purchase_reservation.user_id).order_by("id").first()
        approved, created = approve_purchase_reservation(reservation_id=pk, coach=request.user, approved_for_reservation=source_reservation)
        messages.success(
            request,
            f"現金受領を確認し、{approved.ticket_count}枚のチケットを付与しました。"
            if created else "この購入予約はすでに承認済みです。",
        )
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    lesson_query = request.GET.copy()
    lesson_query.pop("next", None)
    return redirect(
        f"{reverse('club:lesson_calendar_member_list')}?{lesson_query.urlencode()}"
    )


@login_required
@require_http_methods(["GET", "POST"])
def reverse_approval(request, pk):
    if not is_main_coach(request.user):
        raise PermissionDenied
    lesson_reservations = _lesson_reservations(request)
    purchase_reservation = get_object_or_404(
        TicketPurchaseReservation.objects.select_related("user", "ticket_purchase", "approved_by"),
        pk=pk,
    )
    allowed_reservation_ids = set(lesson_reservations.values_list("id", flat=True))
    allowed_user_ids = set(lesson_reservations.exclude(user_id=None).values_list("user_id", flat=True))
    if not (
        purchase_reservation.approved_for_reservation_id in allowed_reservation_ids
        or (purchase_reservation.approved_for_reservation_id is None and purchase_reservation.user_id in allowed_user_ids)
    ):
        raise PermissionDenied
    can_reverse, error = purchase_reversal_availability(purchase_reservation)
    if request.method == "GET":
        if not can_reverse:
            messages.error(request, error)
            return redirect(f"{reverse('club:lesson_calendar_member_list')}?{request.GET.urlencode()}")
        return render(request, "coach/ticket_purchase_reverse_confirm.html", {
            "purchase_reservation": purchase_reservation,
            "reversal_reason_choices": REVERSAL_REASON_CHOICES,
            "back_url": f"{reverse('club:lesson_calendar_member_list')}?{request.GET.urlencode()}",
        })
    try:
        _reversed, changed = reverse_purchase_reservation(
            reservation_id=pk, coach=request.user, reason=request.POST.get("reason", "")
        )
        messages.success(request, "購入承認を取り消し、チケットと売上の計上を戻しました。" if changed else "この購入はすでに承認取消済みです。")
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    return redirect(f"{reverse('club:lesson_calendar_member_list')}?{request.GET.urlencode()}")
