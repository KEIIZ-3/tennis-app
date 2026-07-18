from datetime import datetime
from types import SimpleNamespace

from django.test import SimpleTestCase
from django.utils import timezone

from club.settlement_balance_policy import (
    _automatic_court_cost,
    _lighting_start_hour,
)


class SettlementWalletCourtCostTests(SimpleTestCase):
    def _reservation(self, start_at, end_at, court_count=1):
        return SimpleNamespace(
            start_at=timezone.make_aware(start_at),
            end_at=timezone.make_aware(end_at),
            court_count=court_count,
        )

    def test_lighting_start_hour_by_season(self):
        self.assertEqual(_lighting_start_hour(datetime(2026, 3, 1).date()), 18)
        self.assertEqual(_lighting_start_hour(datetime(2026, 5, 1).date()), 19)
        self.assertEqual(_lighting_start_hour(datetime(2026, 9, 30).date()), 18)
        self.assertEqual(_lighting_start_hour(datetime(2026, 10, 1).date()), 17)
        self.assertEqual(_lighting_start_hour(datetime(2026, 2, 28).date()), 17)

    def test_weekday_two_hour_court_without_lighting(self):
        reservation = self._reservation(
            datetime(2026, 7, 1, 15, 0),
            datetime(2026, 7, 1, 17, 0),
        )

        self.assertEqual(_automatic_court_cost(reservation), 1800)

    def test_weekday_two_hour_court_with_summer_lighting(self):
        reservation = self._reservation(
            datetime(2026, 7, 1, 19, 0),
            datetime(2026, 7, 1, 21, 0),
        )

        self.assertEqual(_automatic_court_cost(reservation), 2600)

    def test_weekend_two_hour_court_with_summer_lighting(self):
        reservation = self._reservation(
            datetime(2026, 7, 4, 19, 0),
            datetime(2026, 7, 4, 21, 0),
        )

        self.assertEqual(_automatic_court_cost(reservation), 3200)

    def test_multiple_courts_are_multiplied(self):
        reservation = self._reservation(
            datetime(2026, 7, 4, 19, 0),
            datetime(2026, 7, 4, 21, 0),
            court_count=2,
        )

        self.assertEqual(_automatic_court_cost(reservation), 6400)
