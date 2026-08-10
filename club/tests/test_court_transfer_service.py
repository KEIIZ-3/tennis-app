from datetime import datetime, time, timedelta

from django.test import TestCase
from django.utils import timezone

from club.court_transfer_service import (
    current_court_transfer_from_expenses,
    get_current_court_transfer_for_availability,
)
from club.expense_metadata import build_expense_note
from club.models import CoachAvailability, CoachExpense, Court, Reservation, User


class CourtTransferServiceTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(
            username="court-transfer-service-coach",
            password="unused",
            role=User.ROLE_COACH,
        )
        court = Court.objects.create(name="Service test court", is_active=True)
        start = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=3), time(10))
        )
        self.availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=court,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=User.LEVEL_BEGINNER,
            start_at=start,
            end_at=start + timedelta(hours=1),
            capacity=1,
        )

    def _expense(self, amount):
        return CoachExpense.objects.create(
            expense_date=timezone.localdate(),
            category=CoachExpense.CATEGORY_COURT,
            amount=amount,
            created_by=self.coach,
            note=build_expense_note(
                {
                    "record_kind": "court_transfer",
                    "availability_id": self.availability.pk,
                    "approval_status": "approved",
                }
            ),
        )

    def test_one_transfer_is_selected_by_database_and_iterable_entry_points(self):
        expense = self._expense(2400)

        self.assertEqual(
            get_current_court_transfer_for_availability(self.availability.pk),
            expense,
        )
        self.assertEqual(
            current_court_transfer_from_expenses([expense], self.availability.pk),
            expense,
        )

    def test_duplicate_fixture_selects_latest_without_deleting_either_row(self):
        oldest = self._expense(2400)
        latest = self._expense(3600)

        self.assertEqual(
            get_current_court_transfer_for_availability(self.availability.pk),
            latest,
        )
        self.assertEqual(
            current_court_transfer_from_expenses(
                [latest, oldest], self.availability.pk
            ),
            latest,
        )
        self.assertEqual(CoachExpense.objects.count(), 2)
