import logging
from contextlib import contextmanager
from time import perf_counter


performance_logger = logging.getLogger("performance")


class LessonMemberListPerformanceTrace:
    """Record safe timings for the lesson member-list GET path."""

    def __init__(self):
        self.started_at = perf_counter()

    @contextmanager
    def step(self, name, *, count=None):
        started_at = perf_counter()
        try:
            yield
        finally:
            duration_ms = round((perf_counter() - started_at) * 1000)
            resolved_count = count() if callable(count) else count
            if resolved_count is None:
                performance_logger.info(
                    "PERFORMANCE lesson_member_list_step step=%s duration_ms=%d",
                    name,
                    duration_ms,
                )
            else:
                performance_logger.info(
                    "PERFORMANCE lesson_member_list_step step=%s duration_ms=%d count=%d",
                    name,
                    duration_ms,
                    int(resolved_count),
                )

    def log_total(self):
        performance_logger.info(
            "PERFORMANCE lesson_member_list_step step=total duration_ms=%d",
            round((perf_counter() - self.started_at) * 1000),
        )
