from unittest.mock import patch

from django.db import connection
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase
from django.test.utils import CaptureQueriesContext, override_settings

from club.middleware import SlowRequestLoggingMiddleware


@override_settings(SLOW_REQUEST_THRESHOLD_MS=500, STATIC_URL="/static/")
class SlowRequestLoggingMiddlewareTests(SimpleTestCase):
    databases = {"default"}

    def setUp(self):
        self.request_factory = RequestFactory()

    def call_middleware(self, path, *, duration_seconds, status=200):
        middleware = SlowRequestLoggingMiddleware(
            lambda request: HttpResponse(status=status)
        )
        request = self.request_factory.get(path)
        with patch(
            "club.middleware.time.perf_counter",
            side_effect=[10.0, 10.0 + duration_seconds],
        ):
            return middleware(request)

    def test_fast_request_is_not_logged(self):
        with self.assertNoLogs("performance", level="WARNING"):
            self.call_middleware("/lesson-calendar/", duration_seconds=0.499)

    def test_slow_request_logs_path_status_and_duration(self):
        with self.assertLogs("performance", level="WARNING") as logs:
            self.call_middleware(
                "/lesson-calendar/?email=private@example.com",
                duration_seconds=0.5,
                status=403,
            )

        message = logs.output[0]
        self.assertIn(
            "PERFORMANCE slow_request path=/lesson-calendar/ "
            "status=403 duration_ms=500",
            message,
        )
        self.assertNotIn("email", message)
        self.assertNotIn("private@example.com", message)

    def test_healthz_is_excluded(self):
        with self.assertNoLogs("performance", level="WARNING"):
            self.call_middleware("/healthz/", duration_seconds=1.0)

    def test_static_file_is_excluded(self):
        with self.assertNoLogs("performance", level="WARNING"):
            self.call_middleware("/static/app.css", duration_seconds=1.0)

    def test_exception_is_not_intercepted(self):
        expected_error = RuntimeError("view failure")

        def raise_error(request):
            raise expected_error

        middleware = SlowRequestLoggingMiddleware(raise_error)

        with self.assertRaises(RuntimeError) as raised:
            middleware(self.request_factory.get("/reservations/"))

        self.assertIs(raised.exception, expected_error)

    def test_middleware_does_not_add_database_queries(self):
        with CaptureQueriesContext(connection) as queries:
            self.call_middleware("/tickets/", duration_seconds=0.1)

        self.assertEqual(len(queries), 0)
