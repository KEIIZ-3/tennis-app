from contextvars import ContextVar


_preopen_level_free_request = ContextVar(
    "preopen_level_free_request",
    default=False,
)


def preopen_level_free_enabled():
    return bool(_preopen_level_free_request.get())


def _request_is_preopen_july(request):
    values = [
        request.GET.get("lesson_date"),
        request.POST.get("lesson_date"),
        request.GET.get("date"),
        request.POST.get("date"),
        request.GET.get("start"),
        request.POST.get("start"),
    ]
    for value in values:
        text = str(value or "").strip()
        if text.startswith(("2026-07", "2026/07", "2026/7")):
            return True
    try:
        year = request.GET.get("year") or request.POST.get("year")
        month = request.GET.get("month") or request.POST.get("month")
        return int(year or 0) == 2026 and int(month or 0) == 7
    except (TypeError, ValueError):
        return False


class PreopenLevelFreeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _preopen_level_free_request.set(
            _request_is_preopen_july(request)
        )
        try:
            return self.get_response(request)
        finally:
            _preopen_level_free_request.reset(token)
