from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from . import settlement_views
from .settlement_models import MonthlySettlement
from .settlement_service import calculate_monthly_settlement


def _selected_month(request):
    today = timezone.localdate()

    try:
        selected_year = int(
            request.GET.get("year")
            or request.POST.get("year")
            or today.year
        )
    except Exception:
        selected_year = today.year

    try:
        selected_month = int(
            request.GET.get("month")
            or request.POST.get("month")
            or today.month
        )
    except Exception:
        selected_month = today.month

    if selected_year < 2000 or selected_year > 2100:
        selected_year = today.year
    if selected_month < 1 or selected_month > 12:
        selected_month = today.month

    return selected_year, selected_month


def _refresh_closed_month(selected_year, selected_month):
    """締め状態と監査情報を維持したまま、最新データで精算内容を再計算する。"""
    with transaction.atomic():
        settlement = (
            MonthlySettlement.objects.select_for_update()
            .filter(year=selected_year, month=selected_month)
            .first()
        )

        if settlement is None or not settlement.is_closed:
            calculate_monthly_settlement(
                selected_year,
                selected_month,
                force=True,
            )
            return

        original_closed_at = settlement.closed_at
        original_closed_by_id = settlement.closed_by_id
        original_reopened_at = settlement.reopened_at
        original_reopened_by_id = settlement.reopened_by_id

        # wallet policy は締め済み月を意図的に処理しないため、再計算中だけ
        # draft として扱う。支払履歴や締め日時などの監査情報は変更しない。
        settlement.status = MonthlySettlement.STATUS_DRAFT
        settlement.updated_at = timezone.now()
        settlement.save(update_fields=["status", "updated_at"])

        calculate_monthly_settlement(
            selected_year,
            selected_month,
            force=True,
        )

        settlement.refresh_from_db()
        settlement.status = MonthlySettlement.STATUS_CLOSED
        settlement.closed_at = original_closed_at
        settlement.closed_by_id = original_closed_by_id
        settlement.reopened_at = original_reopened_at
        settlement.reopened_by_id = original_reopened_by_id
        settlement.updated_at = timezone.now()
        settlement.save(
            update_fields=[
                "status",
                "closed_at",
                "closed_by",
                "reopened_at",
                "reopened_by",
                "updated_at",
            ]
        )


@require_http_methods(["GET", "POST"])
def coach_admin_settlement(request):
    """admin月次精算を表示前に必ず最新データで再計算する。"""
    is_admin = bool(
        getattr(request.user, "is_authenticated", False)
        and (
            getattr(request.user, "is_superuser", False)
            or getattr(request.user, "is_staff", False)
        )
    )
    if not is_admin:
        return HttpResponse("Forbidden", status=403)

    if request.method == "GET":
        selected_year, selected_month = _selected_month(request)
        _refresh_closed_month(selected_year, selected_month)

    return settlement_views.coach_admin_settlement(request)
