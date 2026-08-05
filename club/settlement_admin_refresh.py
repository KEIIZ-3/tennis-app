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
    """Refresh an open month while preserving a closed month's snapshot."""
    settlement = MonthlySettlement.objects.filter(
        year=selected_year,
        month=selected_month,
    ).first()
    if settlement is None or not settlement.is_closed:
        calculate_monthly_settlement(
            selected_year,
            selected_month,
            force=True,
        )


@require_http_methods(["GET", "POST"])
def coach_admin_settlement(request):
    """Refresh open settlement data before rendering the admin view."""
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
