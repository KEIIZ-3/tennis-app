import logging
from contextlib import contextmanager
from time import perf_counter


performance_logger = logging.getLogger("performance")


class SettlementPerformanceTrace:
    def __init__(self, *, enabled, year, month):
        self.enabled = bool(enabled)
        self.year = int(year)
        self.month = int(month)

    @contextmanager
    def step(self, name, *, count=None):
        if not self.enabled:
            yield
            return

        started_at = perf_counter()
        try:
            yield
        finally:
            duration_ms = round((perf_counter() - started_at) * 1000)
            if count is None:
                performance_logger.info(
                    "PERFORMANCE settlement_step step=%s year=%d month=%d duration_ms=%d",
                    name,
                    self.year,
                    self.month,
                    duration_ms,
                )
            else:
                performance_logger.info(
                    "PERFORMANCE settlement_step step=%s year=%d month=%d duration_ms=%d count=%d",
                    name,
                    self.year,
                    self.month,
                    duration_ms,
                    int(count),
                )
