from django.http import HttpResponse
from django.views.decorators.http import require_http_methods

from . import settlement_views


@require_http_methods(["GET", "POST"])
def coach_admin_settlement(request):
    """Authorize access before delegating settlement work to the view."""
    is_admin = bool(
        getattr(request.user, "is_authenticated", False)
        and (
            getattr(request.user, "is_superuser", False)
            or getattr(request.user, "is_staff", False)
        )
    )
    if not is_admin:
        return HttpResponse("Forbidden", status=403)

    return settlement_views.coach_admin_settlement(request)
