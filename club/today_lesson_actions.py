from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import lesson_execution


def _is_allowed(user):
    return bool(
        getattr(user, "is_staff", False)
        or getattr(user, "is_superuser", False)
        or str(getattr(user, "role", "") or "") in ("coach", "contractor_coach")
    )


def _safe_next_url(request):
    candidate = (request.POST.get("next") or "").strip()
    if candidate and url_has_allowed_host_and_scheme(
        url=candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return reverse("club:coach_today_lessons")


@login_required
@require_POST
def lesson_quick_action(request):
    """
    レッスン詳細画面から既存のレッスン実施管理処理を呼び出します。

    実施済み・雨天中止・返金済みの検証、チケット返却、月次精算再計算は
    lesson_execution.lesson_execution_manage を正本として再利用します。
    """
    if not _is_allowed(request.user):
        return HttpResponse("Forbidden", status=403)

    next_url = _safe_next_url(request)
    delegated_post = request.POST.copy()
    delegated_post["year"] = (request.POST.get("year") or "").strip()
    delegated_post["month"] = (request.POST.get("month") or "").strip()
    delegated_post["availability_id"] = (
        request.POST.get("availability_id") or ""
    ).strip()
    delegated_post["action"] = (request.POST.get("action") or "").strip()
    request.POST = delegated_post

    response = lesson_execution.lesson_execution_manage(request)
    if getattr(response, "status_code", None) in (301, 302, 303, 307, 308):
        return redirect(next_url)
    return response
