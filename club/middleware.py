import logging
import time

from django.conf import settings


performance_logger = logging.getLogger("performance")


class SlowRequestLoggingMiddleware:
    """Log slow server-side requests without reading request data."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._is_excluded(request.path):
            return self.get_response(request)

        started_at = time.perf_counter()
        response = self.get_response(request)
        duration_ms = (time.perf_counter() - started_at) * 1000

        if duration_ms >= settings.SLOW_REQUEST_THRESHOLD_MS:
            performance_logger.warning(
                "PERFORMANCE slow_request path=%s status=%s duration_ms=%d",
                request.path,
                response.status_code,
                round(duration_ms),
            )

        return response

    @staticmethod
    def _is_excluded(path):
        static_url = settings.STATIC_URL
        return path == "/healthz/" or (
            static_url.startswith("/") and path.startswith(static_url)
        )
