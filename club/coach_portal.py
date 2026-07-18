from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from . import views


COACH_ROLES = {"coach", "contractor_coach"}


def _is_coach_portal_user(user):
    return bool(
        user.is_authenticated
        and (
            getattr(user, "role", "") in COACH_ROLES
            or getattr(user, "is_staff", False)
            or getattr(user, "is_superuser", False)
        )
    )


def home_dispatch(request):
    """
    会員・未ログイン利用者は従来ホームを維持し、
    コーチ系アカウントだけ業務導線を整理した専用ホームへ送る。
    """
    if not _is_coach_portal_user(request.user):
        return views.home(request)

    today = timezone.localdate()
    return render(
        request,
        "coach/home_v2.html",
        {
            "today": today,
            "current_year": today.year,
            "current_month": today.month,
            "is_admin_user": bool(
                getattr(request.user, "is_staff", False)
                or getattr(request.user, "is_superuser", False)
            ),
        },
    )
