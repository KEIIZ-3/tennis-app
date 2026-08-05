from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from .settlement_integrity_diagnostic import affected_closed_settlements


@login_required
@require_GET
def settlement_integrity_diagnostic(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return HttpResponse("Forbidden", status=403)
    return render(
        request,
        "coach/settlement_integrity_diagnostic.html",
        {"diagnostics": affected_closed_settlements()},
    )
