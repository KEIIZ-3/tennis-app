from datetime import date, datetime, timedelta

from django.test import TestCase
from django.utils import timezone

from club.court_policy_reconciliation import reconcile_court_policy
from club.expense_metadata import build_expense_note
from club.lesson_occurrence_resolver import resolve_authoritative_fixed_lesson
from club.models import CoachAvailability, CoachExpense, Court, FixedLesson, Reservation, User


class LessonOccurrenceResolverTests(TestCase):
    def setUp(self):
        self.fixed_coach = User.objects.create_user(
            username="fixed-coach", role=User.ROLE_COACH
        )
        self.independent_coach = User.objects.create_user(
            username="independent-coach", role=User.ROLE_COACH
        )
        self.payer = User.objects.create_user(
            username="court-payer", role=User.ROLE_COACH
        )
        self.member = User.objects.create_user(username="occurrence-member")
        self.court = Court.objects.create(name="Occurrence Court")
        self.start = timezone.make_aware(datetime(2026, 8, 27, 19))
        self.fixed = FixedLesson.objects.create(
            title="全レベル",
            coach=self.fixed_coach,
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=User.LEVEL_ALL,
            start_date=date(2026, 8, 27),
            weekday=3,
            start_hour=19,
        )

    def availability(self, *, coach=None, level=User.LEVEL_INTERMEDIATE, note="", source=None):
        return CoachAvailability.objects.create(
            coach=coach or self.independent_coach,
            court=self.court,
            fixed_lesson_source=source,
            lesson_type=CoachAvailability.LESSON_GENERAL,
            target_level=level,
            start_at=self.start,
            end_at=self.start + timedelta(hours=2),
            capacity=5,
            note=note,
        )

    def test_same_physical_slot_does_not_convert_independent_availability(self):
        independent = self.availability()

        self.assertIsNone(resolve_authoritative_fixed_lesson(independent))

    def test_explicit_reservation_and_source_are_authoritative(self):
        reservation_linked = self.availability()
        Reservation.objects.create(
            user=self.member,
            coach=self.independent_coach,
            court=self.court,
            availability=reservation_linked,
            fixed_lesson=self.fixed,
            lesson_type=Reservation.LESSON_GENERAL,
            start_at=self.start,
            end_at=self.start + timedelta(hours=2),
        )
        source_linked = self.availability(coach=self.fixed_coach, source=self.fixed)

        self.assertEqual(resolve_authoritative_fixed_lesson(reservation_linked), self.fixed)
        self.assertEqual(resolve_authoritative_fixed_lesson(source_linked), self.fixed)

    def test_unique_legacy_title_and_level_match_is_supported(self):
        legacy = self.availability(
            coach=self.fixed_coach,
            level=User.LEVEL_ALL,
            note="固定レッスン: 全レベル",
        )

        self.assertEqual(resolve_authoritative_fixed_lesson(legacy), self.fixed)

        FixedLesson.objects.create(
            title="全レベル",
            coach=self.fixed_coach,
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=User.LEVEL_ALL,
            start_date=date(2026, 8, 27),
            weekday=3,
            start_hour=19,
        )
        self.assertIsNone(resolve_authoritative_fixed_lesson(legacy))

    def test_court_reconciliation_uses_independent_availability_coach_and_keeps_payer(self):
        independent = self.availability()
        for index in range(4):
            Reservation.objects.create(
                user=User.objects.create_user(username=f"member-{index}"),
                coach=self.independent_coach,
                court=self.court,
                availability=independent,
                lesson_type=Reservation.LESSON_GENERAL,
                start_at=self.start,
                end_at=self.start + timedelta(hours=2),
            )
        expense = CoachExpense.objects.create(
            expense_date=date(2026, 8, 27),
            category=CoachExpense.CATEGORY_COURT,
            amount=2600,
            created_by=self.payer,
            note=build_expense_note({
                "record_kind": "court_transfer",
                "availability_id": independent.pk,
                "payer_coach_id": self.payer.pk,
                "using_coach_ids": [self.independent_coach.pk],
            }),
        )
        policy = {
            "detail_rows": [{
                "expense_id": expense.pk,
                "is_court_transfer": True,
                "amount": 2600,
                "slot_key": f"availability:{independent.pk}",
            }]
        }
        eligible = [self.fixed_coach.pk, self.independent_coach.pk, self.payer.pk]

        result = reconcile_court_policy(
            policy,
            main_coach_ids=eligible,
            eligible_coach_ids=eligible,
            contractor_coach_ids=[],
        )

        row = result["detail_rows"][0]
        self.assertEqual(row["scheduled_fixed_lesson_ids"], [])
        self.assertEqual(row["burden_target_ids"], [self.independent_coach.pk])
        self.assertEqual(result["burden_by_coach"], {self.independent_coach.pk: 2600})
        self.assertEqual(result["reimbursement_by_coach"], {self.payer.pk: 2600})
