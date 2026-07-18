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

    def test_unknown_court_returns_none(self):
        quote = calculate_court_fee(
            DummyCourt("その他", "other"),
            self.aware(2026, 7, 17, 19),
            self.aware(2026, 7, 17, 21),
            1,
        )
        self.assertIsNone(quote)
