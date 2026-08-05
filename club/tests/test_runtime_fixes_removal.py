import importlib
from datetime import date, datetime

from django.apps import apps
from django.test import SimpleTestCase
from django.utils import timezone

from club.apps import ClubConfig
from club.models import CoachAvailability, FixedLesson, Reservation, ShopEstimateRequest


class RuntimeFixesRemovalTests(SimpleTestCase):
    def test_runtime_behaviors_are_defined_on_the_models(self):
        availability = CoachAvailability(
            lesson_type=CoachAvailability.LESSON_GENERAL,
            coach_count=2,
            start_at=timezone.make_aware(datetime(2026, 8, 5, 10, 0)),
        )
        fixed_lesson = FixedLesson(
            lesson_type=FixedLesson.LESSON_GENERAL,
            coach_count=2,
            start_date=date(2026, 8, 5),
        )

        self.assertEqual(availability.effective_capacity(), 10)
        self.assertEqual(fixed_lesson.effective_capacity(), 10)
        self.assertEqual(ShopEstimateRequest.sale_price_from_list_price(10_000), 7_000)
        self.assertEqual(Reservation.consume_tickets.__module__, "club.models")

    def test_model_behavior_does_not_depend_on_app_ready_import_order(self):
        methods_before_ready = (
            CoachAvailability.effective_capacity,
            FixedLesson.effective_capacity,
            ShopEstimateRequest.sale_price_from_list_price,
            Reservation.consume_tickets,
        )

        config = ClubConfig("club", importlib.import_module("club"))
        config.apps = apps
        config.ready()
        config.ready()

        self.assertEqual(
            methods_before_ready,
            (
                CoachAvailability.effective_capacity,
                FixedLesson.effective_capacity,
                ShopEstimateRequest.sale_price_from_list_price,
                Reservation.consume_tickets,
            ),
        )
