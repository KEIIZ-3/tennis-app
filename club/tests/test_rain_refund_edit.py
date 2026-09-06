from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club import lesson_execution
from club.expense_metadata import build_expense_note, parse_expense_note
from club.lesson_execution_storage import save_status
from club.models import CoachAvailability, CoachExpense, Court, RainRefund, Reservation
from club.rain_refund_service import update_pending_rain_refund
from club.settlement_balance_policy import _rain_refund_policy
from club.settlement_models import MonthlySettlement


class RainRefundEditTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.coaches = [
            User.objects.create_user(
                username=f"refund-edit-{index}",
                password="password12345",
                full_name=name,
                role=User.ROLE_COACH,
                is_staff=index == 0,
            )
            for index, name in enumerate(("飯塚研太朗", "清水峻平", "井上春佳"))
        ]
        self.member = User.objects.create_user(
            username="refund-edit-member",
            role=User.ROLE_MEMBER,
        )
        self.court = Court.objects.create(name="返金情報修正コート", is_active=True)
        start_at = timezone.make_aware(datetime(2026, 8, 17, 19))
        self.availability = CoachAvailability.objects.create(
            coach=self.coaches[0],
            court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=start_at + timedelta(hours=2),
            capacity=1,
        )
        self.reservation = Reservation.objects.create(
            user=self.member,
            coach=self.coaches[0],
            court=self.court,
            availability=self.availability,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=start_at + timedelta(hours=2),
            status=Reservation.STATUS_RAIN_CANCELED,
            cancellation_reason="雨天中止",
        )
        self.expense = CoachExpense.objects.create(
            expense_date=start_at.date(),
            category=CoachExpense.CATEGORY_COURT,
            amount=2400,
            created_by=self.coaches[0],
            note=build_expense_note(
                {
                    "expense_type": "court_transfer",
                    "approval_status": "refund_pending",
                    "record_kind": "cancellation_court_settlement",
                    "availability_id": self.availability.pk,
                    "rain_refund_account_kind": "coach",
                    "rain_refund_account_coach_id": self.coaches[0].pk,
                    "rain_refund_account_name": self.coaches[0].display_name(),
                    "rain_refund_account_other": "",
                    "rain_refund_collection_coach_id": self.coaches[1].pk,
                    "rain_refund_collection_coach_name": self.coaches[1].display_name(),
                    "rain_refund_payer_coach_id": self.coaches[0].pk,
                    "rain_refund_payer_coach_name": self.coaches[0].display_name(),
                    "rain_refund_debit_coach_id": self.coaches[0].pk,
                    "rain_refund_debit_coach_name": self.coaches[0].display_name(),
                },
                "中止時コート精算",
            ),
        )
        self.refund = RainRefund.objects.create(
            expense=self.expense,
            availability=self.availability,
            lesson_date=start_at.date(),
            lesson_label="返金情報修正レッスン",
            amount=2400,
            status=RainRefund.STATUS_PENDING,
            booking_account_kind=RainRefund.ACCOUNT_COACH,
            booking_account_coach=self.coaches[0],
            collection_coach=self.coaches[1],
            debit_coach=self.coaches[0],
            payer_coach=self.coaches[0],
        )
        self.settlement = MonthlySettlement.objects.create(year=2026, month=8)
        save_status(
            self.settlement,
            lesson_execution._availability_key(self.availability),
            lesson_execution.STATUS_REFUND_PENDING,
            self.coaches[0],
        )

    def _new_input(self):
        return {
            "account_kind": RainRefund.ACCOUNT_OTHER,
            "account_coach": None,
            "account_other": "公園予約ID",
            "collection_coach": self.coaches[2],
            "debit_coach": self.coaches[2],
            "payer_coach": self.coaches[1],
        }

    def _post_data(self):
        return {
            "year": 2026,
            "month": 8,
            "availability_id": self.availability.pk,
            "action": "update_rain_refund",
            "rain_booking_account": "other",
            "rain_booking_account_other": "公園予約ID",
            "rain_collection_coach_id": self.coaches[2].pk,
            "rain_court_payer_id": self.coaches[1].pk,
        }

    def test_pending_card_shows_edit_button_and_existing_values(self):
        self.client.force_login(self.coaches[0])
        response = self.client.get(
            reverse("club:lesson_execution_manage"),
            {"year": 2026, "month": 8, "open_refund_edit": self.availability.pk},
        )

        self.assertContains(response, "中止情報を修正して保存")
        self.assertContains(response, f'value="{self.coaches[0].pk}" selected')
        self.assertContains(response, f'value="{self.coaches[1].pk}" selected')

    def test_update_changes_both_representations_without_amounts(self):
        update_pending_rain_refund(self.availability.pk, refund_input=self._new_input())

        self.refund.refresh_from_db()
        self.expense.refresh_from_db()
        meta = parse_expense_note(self.expense.note)
        self.assertEqual(self.refund.booking_account_kind, RainRefund.ACCOUNT_OTHER)
        self.assertIsNone(self.refund.booking_account_coach)
        self.assertEqual(self.refund.booking_account_other, "公園予約ID")
        self.assertEqual(self.refund.collection_coach, self.coaches[2])
        self.assertEqual(self.refund.debit_coach, self.coaches[2])
        self.assertEqual(self.refund.payer_coach, self.coaches[1])
        self.assertEqual(meta["rain_refund_account_name"], "公園予約ID")
        self.assertEqual(meta["rain_refund_collection_coach_id"], self.coaches[2].pk)
        self.assertEqual(meta["rain_refund_payer_coach_id"], self.coaches[1].pk)
        self.assertEqual(meta["rain_refund_debit_coach_id"], self.coaches[2].pk)
        self.assertEqual(self.expense.created_by, self.coaches[1])
        self.assertEqual(self.refund.amount, 2400)
        self.assertEqual(self.expense.amount, 2400)

    def test_post_updates_display_and_monthly_policy_only_for_target(self):
        other_availability = CoachAvailability.objects.create(
            coach=self.coaches[0], court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=get_user_model().LEVEL_BEGINNER,
            start_at=self.availability.start_at + timedelta(days=1),
            end_at=self.availability.end_at + timedelta(days=1), capacity=1,
        )
        other_expense = CoachExpense.objects.create(
            expense_date=self.expense.expense_date + timedelta(days=1),
            category=CoachExpense.CATEGORY_COURT, amount=1800,
            created_by=self.coaches[0], note=build_expense_note({"approval_status": "refund_pending"}),
        )
        other_refund = RainRefund.objects.create(
            expense=other_expense, availability=other_availability,
            lesson_date=other_expense.expense_date, amount=1800,
            booking_account_kind=RainRefund.ACCOUNT_COACH,
            booking_account_coach=self.coaches[0], debit_coach=self.coaches[0],
            payer_coach=self.coaches[0],
        )
        self.client.force_login(self.coaches[0])

        response = self.client.post(
            reverse("club:lesson_execution_manage"), self._post_data(), follow=True
        )

        self.assertContains(response, "予約アカウント 公園予約ID")
        self.assertContains(response, f"回収予定 {self.coaches[2].display_name()}")
        self.assertContains(response, f"コート支払者 {self.coaches[1].display_name()}")
        other_refund.refresh_from_db()
        self.assertEqual(other_refund.payer_coach, self.coaches[0])
        policy = _rain_refund_policy(2026, 8, [coach.pk for coach in self.coaches])
        self.assertEqual(policy["pending_total"], 4200)

    @patch("club.reservation_notification_service.schedule_occurrence_rain_canceled_notifications")
    def test_edit_does_not_change_reservation_ticket_or_send_notification(self, notify):
        before = list(Reservation.objects.filter(pk=self.reservation.pk).values())
        self.client.force_login(self.coaches[0])
        self.client.post(reverse("club:lesson_execution_manage"), self._post_data())

        self.assertEqual(
            list(Reservation.objects.filter(pk=self.reservation.pk).values()), before
        )
        notify.assert_not_called()

    def test_refunded_is_hidden_and_direct_post_is_rejected(self):
        self.refund.status = RainRefund.STATUS_REFUNDED
        self.refund.save(update_fields=["status", "updated_at"])
        self.client.force_login(self.coaches[0])
        response = self.client.get(
            reverse("club:lesson_execution_manage"), {"year": 2026, "month": 8}
        )
        self.assertNotContains(response, "中止情報を修正")

        response = self.client.post(
            reverse("club:lesson_execution_manage"), self._post_data(), follow=True
        )
        self.assertContains(response, "返金待ちの雨天中止精算情報だけを修正できます")

    def test_closed_month_rejects_edit(self):
        self.settlement.status = MonthlySettlement.STATUS_CLOSED
        self.settlement.save(update_fields=["status"])
        self.client.force_login(self.coaches[0])

        response = self.client.post(
            reverse("club:lesson_execution_manage"), self._post_data(), follow=True
        )

        self.assertContains(response, "締め済みの月は開催状態を変更できません")
        self.refund.refresh_from_db()
        self.assertEqual(self.refund.booking_account_kind, RainRefund.ACCOUNT_COACH)

    def test_invalid_input_changes_neither_record(self):
        self.client.force_login(self.coaches[0])
        data = self._post_data()
        data["rain_court_payer_id"] = "invalid"
        before_note = self.expense.note

        self.client.post(reverse("club:lesson_execution_manage"), data)

        self.refund.refresh_from_db()
        self.expense.refresh_from_db()
        self.assertEqual(self.refund.booking_account_kind, RainRefund.ACCOUNT_COACH)
        self.assertEqual(self.expense.note, before_note)

    def test_expense_failure_rolls_back_refund(self):
        with patch.object(RainRefund, "save", side_effect=RuntimeError("write failed")):
            with self.assertRaisesMessage(RuntimeError, "write failed"):
                update_pending_rain_refund(
                    self.availability.pk, refund_input=self._new_input()
                )

        self.refund.refresh_from_db()
        self.expense.refresh_from_db()
        self.assertEqual(self.refund.booking_account_kind, RainRefund.ACCOUNT_COACH)
        self.assertEqual(self.expense.created_by, self.coaches[0])
        self.assertEqual(
            parse_expense_note(self.expense.note)["rain_refund_account_kind"],
            "coach",
        )

    def test_member_has_no_edit_permission(self):
        self.client.force_login(self.member)
        response = self.client.post(
            reverse("club:lesson_execution_manage"), self._post_data()
        )
        self.assertEqual(response.status_code, 403)
