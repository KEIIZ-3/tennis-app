from datetime import datetime, timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from club.lesson_execution_storage import save_status
from club.models import (
    CoachAvailability,
    CoachExpense,
    Court,
    RainRefund,
    Reservation,
    User,
)
from club.settlement_models import MonthlySettlement


class CoachAvailabilityOverlapValidationTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(
            username="availability-overlap-coach", role=User.ROLE_COACH
        )
        self.other_coach = User.objects.create_user(
            username="availability-overlap-other-coach", role=User.ROLE_COACH
        )
        self.member = User.objects.create_user(
            username="availability-overlap-member", role=User.ROLE_MEMBER
        )
        self.court = Court.objects.create(
            name="Availability validation court", available_court_count=12
        )
        self.other_court = Court.objects.create(name="Availability validation other court")
        self.start_at = timezone.make_aware(datetime(2026, 8, 24, 10))

    def create_availability(
        self, *, coach=None, court=None, start_at=None, end_at=None,
        target_level=User.LEVEL_BEGINNER, is_recruitment_closed=False,
    ):
        start_at = start_at or self.start_at
        return CoachAvailability.objects.create(
            coach=coach or self.coach,
            court=court or self.court,
            lesson_type=CoachAvailability.LESSON_PRIVATE,
            target_level=target_level,
            start_at=start_at,
            end_at=end_at or start_at + timedelta(hours=1),
            capacity=1,
            is_recruitment_closed=is_recruitment_closed,
        )

    def set_court_count(self, availability, court_count):
        CoachAvailability.objects.filter(pk=availability.pk).update(court_count=court_count)
        availability.refresh_from_db()
        return availability

    def cancel_availability(self, availability, *, execution_status, refund_status=None):
        Reservation.objects.create(
            user=self.member,
            coach=availability.coach,
            court=availability.court,
            availability=availability,
            lesson_type=availability.lesson_type,
            target_level=availability.target_level,
            start_at=availability.start_at,
            end_at=availability.end_at,
            status=Reservation.STATUS_RAIN_CANCELED,
            cancellation_reason="雨天中止",
        )
        settlement, _created = MonthlySettlement.objects.get_or_create(
            year=availability.start_at.year,
            month=availability.start_at.month,
        )
        save_status(
            settlement,
            f"availability:{availability.pk}",
            execution_status,
            self.coach,
            cancellation_type="rain",
        )
        if refund_status:
            expense = CoachExpense.objects.create(
                expense_date=availability.start_at.date(),
                category=CoachExpense.CATEGORY_COURT,
                amount=1000,
                created_by=self.coach,
            )
            RainRefund.objects.create(
                expense=expense,
                availability=availability,
                lesson_date=availability.start_at.date(),
                amount=1000,
                status=refund_status,
                booking_account_kind=RainRefund.ACCOUNT_COACH,
                booking_account_coach=self.coach,
                debit_coach=self.coach,
            )

    def test_same_coach_overlapping_time_raises_validation_error(self):
        self.create_availability()

        with self.assertRaisesMessage(
            ValidationError,
            "同じコーチで重複する空き時間があります。",
        ):
            self.create_availability(
                start_at=self.start_at,
                end_at=self.start_at + timedelta(hours=2),
            )

    def test_different_coach_and_court_can_use_same_time(self):
        self.create_availability()

        availability = self.create_availability(coach=self.other_coach, court=self.other_court)

        self.assertIsNotNone(availability.pk)

    def test_different_coach_can_share_court_within_capacity(self):
        self.create_availability()
        self.assertIsNotNone(self.create_availability(coach=self.other_coach).pk)

    def test_different_level_can_share_court_within_capacity(self):
        self.create_availability()
        availability = self.create_availability(
            coach=self.other_coach, target_level=User.LEVEL_INTERMEDIATE
        )
        self.assertIsNotNone(availability.pk)

    def test_total_equal_to_court_capacity_is_allowed(self):
        self.set_court_count(self.create_availability(), 11)
        self.assertIsNotNone(self.create_availability(coach=self.other_coach).pk)

    def test_total_over_court_capacity_is_rejected_with_usage_details(self):
        self.set_court_count(self.create_availability(), 11)
        third_coach = User.objects.create_user(
            username="availability-overlap-third-coach", role=User.ROLE_COACH
        )

        with self.assertRaisesMessage(
            ValidationError, "利用中 11面 / 追加 2面 / 利用可能 12面"
        ):
            CoachAvailability.objects.create(
                coach=self.other_coach, coach_2=third_coach, court=self.court,
                lesson_type=CoachAvailability.LESSON_GENERAL,
                target_level=User.LEVEL_BEGINNER,
                start_at=self.start_at, end_at=self.start_at + timedelta(hours=2),
            )

    def test_same_coach_can_use_non_overlapping_time(self):
        self.create_availability()

        availability = self.create_availability(
            start_at=self.start_at + timedelta(hours=1)
        )

        self.assertIsNotNone(availability.pk)

    def test_edit_does_not_treat_itself_as_overlap(self):
        availability = self.create_availability()
        self.court.available_court_count = 1
        self.court.save(update_fields=["available_court_count"])
        availability.target_level_2 = availability.target_level

        availability.save(update_fields=["target_level_2"])

        availability.refresh_from_db()
        self.assertEqual(availability.target_level_2, "")

    def test_non_overlapping_availability_does_not_consume_court_capacity(self):
        self.set_court_count(self.create_availability(), 12)
        availability = self.create_availability(
            coach=self.other_coach, start_at=self.start_at + timedelta(hours=1)
        )
        self.assertIsNotNone(availability.pk)

    def test_recruitment_closed_availability_still_consumes_court_capacity(self):
        self.set_court_count(self.create_availability(is_recruitment_closed=True), 12)
        with self.assertRaisesMessage(ValidationError, "利用可能コート面数を超えています"):
            self.create_availability(coach=self.other_coach)

    def test_all_availability_statuses_consume_court_capacity(self):
        statuses = (
            CoachAvailability.STATUS_OPEN,
            CoachAvailability.STATUS_REQUESTED,
            CoachAvailability.STATUS_APPROVED,
        )
        for index, status in enumerate(statuses):
            start_at = self.start_at + timedelta(days=index)
            existing = self.create_availability(start_at=start_at)
            CoachAvailability.objects.filter(pk=existing.pk).update(
                court_count=12, status=status
            )
            with self.assertRaisesMessage(ValidationError, "利用可能コート面数を超えています"):
                self.create_availability(coach=self.other_coach, start_at=start_at)

    def test_canceled_execution_statuses_do_not_block_same_coach(self):
        cases = (
            ("rain_canceled", None),
            ("refund_pending", RainRefund.STATUS_PENDING),
            ("refunded", RainRefund.STATUS_REFUNDED),
            ("refund_pending", RainRefund.STATUS_VOIDED),
        )
        for index, (execution_status, refund_status) in enumerate(cases):
            start_at = self.start_at + timedelta(days=index)
            canceled = self.create_availability(start_at=start_at)
            self.cancel_availability(
                canceled,
                execution_status=execution_status,
                refund_status=refund_status,
            )
            replacement = self.create_availability(start_at=start_at)
            self.assertIsNotNone(replacement.pk)

    def test_canceled_availability_does_not_consume_court_capacity(self):
        canceled = self.set_court_count(self.create_availability(), 12)
        self.cancel_availability(canceled, execution_status="rain_canceled")

        replacement = self.create_availability(coach=self.other_coach)

        self.assertIsNotNone(replacement.pk)

    def test_rain_refund_record_alone_does_not_disable_overlap_validation(self):
        existing = self.create_availability()
        expense = CoachExpense.objects.create(
            expense_date=existing.start_at.date(),
            category=CoachExpense.CATEGORY_COURT,
            amount=1000,
            created_by=self.coach,
        )
        RainRefund.objects.create(
            expense=expense,
            availability=existing,
            lesson_date=existing.start_at.date(),
            amount=1000,
            status=RainRefund.STATUS_VOIDED,
            booking_account_kind=RainRefund.ACCOUNT_COACH,
            booking_account_coach=self.coach,
            debit_coach=self.coach,
        )

        with self.assertRaisesMessage(
            ValidationError,
            "同じコーチで重複する空き時間があります。",
        ):
            self.create_availability()
