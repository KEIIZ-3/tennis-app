from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from .models import Reservation


def _can_cancel_reservation(user, reservation):
    if not user or not getattr(user, "is_authenticated", False):
        return False

    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True

    role = getattr(user, "role", "")
    if role == "member":
        return reservation.user_id == getattr(user, "pk", None)

    return False


@login_required
@require_POST
def reservation_cancel(request, pk):
    reservation = get_object_or_404(
        Reservation.objects.select_related(
            "user",
            "coach",
            "substitute_coach",
            "court",
            "availability",
            "fixed_lesson",
        ),
        pk=pk,
    )

    if not _can_cancel_reservation(request.user, reservation):
        return HttpResponse("Forbidden", status=403)

    if reservation.status not in (
        Reservation.STATUS_ACTIVE,
        Reservation.STATUS_PENDING,
    ):
        messages.info(
            request,
            "この予約はすでにキャンセル済み、またはキャンセルできない状態です。",
        )
        return redirect("club:reservation_detail", pk=reservation.pk)

    actor_role = getattr(request.user, "role", "")
    if actor_role == "member":
        reason = "会員が予約詳細画面からキャンセル"
    else:
        reason = "コーチまたは管理者が予約詳細画面からキャンセル"

    try:
        canceled = reservation.cancel(
            created_by=request.user,
            reason=reason,
        )
        if canceled:
            messages.success(
                request,
                "予約をキャンセルしました。消費済みチケットは返却しました。",
            )
        else:
            messages.info(request, "予約状態に変更はありませんでした。")
    except ValidationError as exc:
        for message in getattr(exc, "messages", [str(exc)]):
            messages.error(request, message)
    except Exception as exc:
        messages.error(request, f"予約のキャンセルに失敗しました: {exc}")

    return redirect("club:reservation_detail", pk=reservation.pk)
