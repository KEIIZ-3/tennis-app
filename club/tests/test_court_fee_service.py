from datetime import datetime

from django.test import SimpleTestCase
from django.utils import timezone

from club.court_fee_service import calculate_court_fee


class DummyCourt:
    def __init__(self, name, court_type):
        self.name = name
        self.court_type = court_type

    def __str__(self):
        return self.name


class CourtFeeServiceTests(SimpleTestCase):
    def aware(self, year, month, day, hour, minute=0):
        return timezone.make_aware(datetime(year, month, day, hour, minute))

    def test_amagasaki_weekday_evening(self):
        quote = calculate_court_fee(
            DummyCourt("尼崎記念公園", "amagasaki"),
            self.aware(2026, 7, 17, 19),
            self.aware(2026, 7, 17, 21),
            1,
        )
        self.assertEqual(quote["total"], 2200)

    def test_amagasaki_sunday_evening(self):
        quote = calculate_court_fee(
            DummyCourt("尼崎記念公園", "amagasaki"),
            self.aware(2026, 7, 19, 19),
            self.aware(2026, 7, 19, 21),
            1,
        )
        self.assertEqual(quote["total"], 2560)

    def test_sono_saturday_evening_two_courts(self):
        quote = calculate_court_fee(
            DummyCourt("西猪名公園", "sono"),
            self.aware(2026, 7, 18, 19),
            self.aware(2026, 7, 18, 21),
            2,
        )
        self.assertEqual(quote["total"], 6400)

    def test_sono_seasonal_lighting_boundaries(self):
        cases = (
            ((2026, 2, 28), 17),
            ((2026, 3, 1), 18),
            ((2026, 4, 30), 18),
            ((2026, 5, 1), 19),
            ((2026, 8, 31), 19),
            ((2026, 9, 1), 18),
            ((2026, 9, 30), 18),
            ((2026, 10, 1), 17),
            ((2026, 12, 31), 17),
            ((2027, 1, 1), 17),
        )
        court = DummyCourt("西猪名公園", "sono")
        for (year, month, day), lighting_hour in cases:
            with self.subTest(date=(year, month, day)):
                quote = calculate_court_fee(
                    court,
                    self.aware(year, month, day, lighting_hour),
                    self.aware(year, month, day, lighting_hour + 1),
                )
                self.assertEqual(quote["lighting_minutes"], 60)

    def test_sono_october_partial_lighting_overlap(self):
        court = DummyCourt("西猪名公園", "sono")
        for start_hour, end_hour, expected_minutes in (
            (16, 18, 60),
            (17, 19, 120),
            (18, 20, 120),
        ):
            with self.subTest(start_hour=start_hour):
                quote = calculate_court_fee(
                    court,
                    self.aware(2026, 10, 2, start_hour),
                    self.aware(2026, 10, 2, end_hour),
                )
                self.assertEqual(quote["lighting_minutes"], expected_minutes)

    def test_sono_october_weekday_and_weekend_totals(self):
        court = DummyCourt("西猪名公園", "sono")
        weekday = calculate_court_fee(
            court,
            self.aware(2026, 10, 2, 18),
            self.aware(2026, 10, 2, 20),
        )
        saturday = calculate_court_fee(
            court,
            self.aware(2026, 10, 3, 18),
            self.aware(2026, 10, 3, 20),
        )
        sunday_two_courts = calculate_court_fee(
            court,
            self.aware(2026, 10, 4, 18),
            self.aware(2026, 10, 4, 20),
            2,
        )
        holiday = calculate_court_fee(
            court,
            self.aware(2026, 10, 12, 18),
            self.aware(2026, 10, 12, 20),
        )
        self.assertEqual(weekday["total"], 2600)
        self.assertEqual(saturday["total"], 3200)
        self.assertEqual(sunday_two_courts["total"], 6400)
        self.assertEqual(holiday["total"], 3200)

    def test_sono_october_sunday_19_to_21_is_3200(self):
        quote = calculate_court_fee(
            DummyCourt("Nishi-Ina Park", "sono"),
            self.aware(2026, 10, 11, 19),
            self.aware(2026, 10, 11, 21),
            1,
        )
        self.assertEqual(quote["base_amount"], 2400)
        self.assertEqual(quote["lighting_amount"], 800)
        self.assertEqual(quote["total"], 3200)

    def test_unknown_court_returns_none(self):
        quote = calculate_court_fee(
            DummyCourt("その他", "other"),
            self.aware(2026, 7, 17, 19),
            self.aware(2026, 7, 17, 21),
            1,
        )
        self.assertIsNone(quote)
