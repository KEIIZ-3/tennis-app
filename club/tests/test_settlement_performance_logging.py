from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext

from club.settlement_performance import SettlementPerformanceTrace


class SettlementPerformanceLoggingTests(SimpleTestCase):
    def test_step_log_contains_only_operational_metadata(self):
        trace = SettlementPerformanceTrace(enabled=True, year=2026, month=8)

        with self.assertLogs("performance", level="INFO") as logs:
            with trace.step("reservations_aggregate", count=3):
                pass

        message = logs.output[0]
        self.assertIn("PERFORMANCE settlement_step", message)
        self.assertIn("step=reservations_aggregate", message)
        self.assertIn("year=2026 month=8", message)
        self.assertIn("count=3", message)
        for personal_value in ("member@example.com", "090-0000-0000", "coach_name"):
            self.assertNotIn(personal_value, message)

    def test_timing_step_does_not_execute_database_queries(self):
        trace = SettlementPerformanceTrace(enabled=True, year=2026, month=8)

        with (
            patch("club.settlement_performance.performance_logger.info"),
            CaptureQueriesContext(connection) as queries,
        ):
            with trace.step("wallet_policy"):
                pass

        self.assertEqual(len(queries), 0)

    def test_disabled_trace_does_not_log(self):
        trace = SettlementPerformanceTrace(enabled=False, year=2026, month=8)

        with patch("club.settlement_performance.performance_logger.info") as log:
            with trace.step("wallet_policy"):
                pass

        log.assert_not_called()
