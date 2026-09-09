from datetime import datetime, timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.completed_lesson_registration import cancel_completed_lesson, register_completed_lesson
from club.lesson_calendar_service import build_lesson_calendar_display_data
from club.lesson_ticket_rules import standard_ticket_count
from club.lesson_execution_storage import read_status_map, save_status
from club.models import (
    CoachAvailability, CoachExpense, CompletedLessonRegistration, Court, Reservation,
    TicketConsumption, TicketLedger, TicketPurchase, User,
)
from club.settlement_models import MonthlySettlement


class CompletedLessonRegistrationTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(username="completed-coach", role=User.ROLE_COACH)
        self.member1 = User.objects.create_user(username="completed-member-1", role=User.ROLE_MEMBER, ticket_balance=5)
        self.member2 = User.objects.create_user(username="completed-member-2", role=User.ROLE_MEMBER, ticket_balance=4)
        self.court = Court.objects.create(name="事後登録コート", available_court_count=2)
        self.start = timezone.make_aware(datetime(2026, 9, 6, 10, 0))
        self.end = self.start + timedelta(hours=1)
        self.lot1 = TicketPurchase.objects.create(user=self.member1, total_tickets=5, remaining_tickets=5, unit_price=2000, purchased_at=self.start-timedelta(days=1))
        self.lot2 = TicketPurchase.objects.create(user=self.member2, total_tickets=4, remaining_tickets=4, unit_price=2500, purchased_at=self.start-timedelta(days=1))

    def register_private(self, *, key="completed-private", participants=None, court_cost=3000):
        return register_completed_lesson(
            actor=self.coach, start_at=self.start, end_at=self.end,
            lesson_type=Reservation.LESSON_PRIVATE, coach=self.coach, court=self.court,
            participants=participants or [
                {"user": self.member1, "payment_method": "ticket", "value": 2},
                {"user": self.member2, "payment_method": "ticket", "value": 1},
            ], court_cost=court_cost, court_payer=self.coach, note="急な依頼", idempotency_key=key,
        )

    def test_private_two_members_uses_canonical_ticket_evidence_and_is_held(self):
        registration, created = self.register_private()
        self.assertTrue(created)
        reservations = list(Reservation.objects.filter(availability=registration.availability).order_by("id"))
        self.assertEqual([row.tickets_used for row in reservations], [2, 1])
        self.assertEqual(TicketConsumption.objects.filter(reservation__in=reservations, refunded_at__isnull=True).count(), 2)
        self.assertEqual(TicketLedger.objects.filter(reservation__in=reservations, change_amount__lt=0).count(), 2)
        self.member1.refresh_from_db(); self.member2.refresh_from_db()
        self.assertEqual((self.member1.ticket_balance, self.member2.ticket_balance), (3, 3))
        settlement = MonthlySettlement.objects.get(year=2026, month=9)
        self.assertEqual(read_status_map(settlement)[f"availability:{registration.availability_id}"]["status"], "held")
        self.assertTrue(registration.availability.is_recruitment_closed)

    def test_ticket_cash_guest_mix_and_idempotent_post(self):
        participants = [
            {"user": self.member1, "payment_method": "ticket", "value": 2},
            {"user": None, "guest_name": "ゲスト一郎", "payment_method": "cash", "value": 4000},
        ]
        registration, created = self.register_private(key="mixed", participants=participants)
        repeated, repeated_created = self.register_private(key="mixed", participants=participants)
        self.assertTrue(created); self.assertFalse(repeated_created)
        self.assertEqual(repeated.pk, registration.pk)
        self.assertEqual(Reservation.objects.filter(availability=registration.availability).count(), 2)
        guest = Reservation.objects.get(availability=registration.availability, user__isnull=True)
        self.assertEqual((guest.guest_name, guest.payment_method, guest.payment_amount), ("ゲスト一郎", "cash", 4000))

    def test_completed_availability_is_one_calendar_card_for_five_participants(self):
        participants = [
            {"user": None, "guest_name": f"ゲスト{i}", "payment_method": "cash", "value": 1000}
            for i in range(5)
        ]
        registration, _created = self.register_private(
            key="five-participants", participants=participants, court_cost=0
        )
        self.client.force_login(self.coach)
        response = self.client.get(reverse("club:lesson_calendar"), {"year": 2026, "month": 9})
        cards = [
            row for row in response.context["schedule_rows"]
            if row["availability_id"] == str(registration.availability_id)
        ]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["customer_status_label"], "実施済み 5/5名")
        member_response = self.client.get(
            reverse("club:lesson_calendar_member_list"),
            {"availability_id": registration.availability_id},
        )
        for index in range(5):
            self.assertContains(member_response, f"ゲスト{index}")

    def test_ticket_rule_helper_matches_reservation_canonical_rules(self):
        self.assertEqual(standard_ticket_count(lesson_type="private", duration_hours=2), 4)
        self.assertEqual(standard_ticket_count(lesson_type="private", duration_hours=1), 2)
        self.assertEqual(standard_ticket_count(lesson_type="group", duration_hours=2, participant_count=2), 4)
        self.assertEqual(standard_ticket_count(lesson_type="general", duration_hours=2), 1)
        self.assertEqual(standard_ticket_count(lesson_type="event", duration_hours=2), 0)

    def test_non_positive_duration_is_rejected_server_side(self):
        with self.assertRaisesMessage(ValidationError, "開始時刻は終了時刻より前にしてください"):
            register_completed_lesson(
                actor=self.coach, start_at=self.start, end_at=self.start,
                lesson_type=Reservation.LESSON_PRIVATE, coach=self.coach, court=self.court,
                participants=[{"user": self.member1, "payment_method": "ticket", "value": 1}],
                court_cost=0, court_payer=self.coach, note="", idempotency_key="invalid-time",
            )

    def test_guest_ticket_duplicate_member_future_and_permissions_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.register_private(key="guest-ticket", participants=[{"user": None, "guest_name": "G", "payment_method": "ticket", "value": 1}])
        with self.assertRaises(ValidationError):
            self.register_private(key="duplicate", participants=[{"user": self.member1, "payment_method": "ticket", "value": 1}] * 2)
        member_actor = User.objects.create_user(username="member-actor", role=User.ROLE_MEMBER)
        with self.assertRaises(ValidationError):
            register_completed_lesson(actor=member_actor, start_at=self.start, end_at=self.end, lesson_type="private", coach=self.coach, court=self.court, participants=[{"user": self.member1,"payment_method":"ticket","value":1}], court_cost=0, court_payer=self.coach, note="", idempotency_key="forbidden")

    def test_cancel_refunds_tickets_reverses_expense_status_and_is_idempotent(self):
        registration, _ = self.register_private()
        expense_count = CoachExpense.objects.count()
        _row, changed = cancel_completed_lesson(registration_id=registration.pk, actor=self.coach)
        self.assertTrue(changed)
        self.member1.refresh_from_db(); self.member2.refresh_from_db(); self.lot1.refresh_from_db(); self.lot2.refresh_from_db()
        self.assertEqual((self.member1.ticket_balance, self.member2.ticket_balance), (5, 4))
        self.assertEqual((self.lot1.remaining_tickets, self.lot2.remaining_tickets), (5, 4))
        self.assertFalse(Reservation.objects.filter(availability=registration.availability, status=Reservation.STATUS_ACTIVE).exists())
        self.assertEqual(TicketConsumption.objects.filter(reservation__availability=registration.availability, refunded_at__isnull=True).count(), 0)
        self.assertEqual(CoachExpense.objects.count(), expense_count + 1)
        settlement = MonthlySettlement.objects.get(year=2026, month=9)
        self.assertNotIn(f"availability:{registration.availability_id}", read_status_map(settlement))
        _row, second_changed = cancel_completed_lesson(registration_id=registration.pk, actor=self.coach)
        self.assertFalse(second_changed)
        self.member1.refresh_from_db(); self.assertEqual(self.member1.ticket_balance, 5)

    def test_closed_month_blocks_registration_and_cancel(self):
        registration, _ = self.register_private()
        settlement = MonthlySettlement.objects.get(year=2026, month=9)
        settlement.status = MonthlySettlement.STATUS_CLOSED; settlement.save(update_fields=["status"])
        with self.assertRaises(ValidationError):
            cancel_completed_lesson(registration_id=registration.pk, actor=self.coach)

    def test_other_lesson_types_and_three_participants(self):
        for offset, lesson_type, hours in [(1, "general", 2), (2, "group", 1), (3, "event", 1)]:
            start = self.start + timedelta(hours=offset * 3)
            registration, _ = register_completed_lesson(
                actor=self.coach, start_at=start, end_at=start+timedelta(hours=hours),
                lesson_type=lesson_type, coach=self.coach, court=self.court,
                participants=[{"user": None, "guest_name": f"G{i}", "payment_method": "cash", "value": 1000} for i in range(3)],
                court_cost=0, court_payer=self.coach, note="", idempotency_key=f"type-{lesson_type}",
            )
            self.assertEqual(Reservation.objects.filter(availability=registration.availability, status="active").count(), 3)

    def _canceled_overlap(self, *, status="refund_pending", cancellation_type="rain"):
        availability = CoachAvailability.objects.create(
            coach=self.coach, court=self.court, lesson_type=Reservation.LESSON_PRIVATE,
            start_at=self.start, end_at=self.end, capacity=1, target_level=User.LEVEL_ALL,
        )
        reservation = Reservation.objects.create(
            user=self.member1, coach=self.coach, court=self.court, availability=availability,
            lesson_type=Reservation.LESSON_PRIVATE, target_level=User.LEVEL_ALL,
            start_at=self.start, end_at=self.end, status=Reservation.STATUS_CANCELED,
            cancellation_reason="雨天中止" if cancellation_type == "rain" else "レッスン中止",
        )
        settlement, _created = MonthlySettlement.objects.get_or_create(year=2026, month=9)
        save_status(settlement, f"availability:{availability.pk}", status, self.coach,
                    cancellation_type=cancellation_type)
        return availability, reservation

    def test_canonically_rain_canceled_coach_and_court_overlap_is_excluded(self):
        canceled, reservation = self._canceled_overlap()
        registration, created = self.register_private(key="after-rain")
        self.assertTrue(created)
        self.assertNotEqual(registration.availability_id, canceled.pk)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.STATUS_CANCELED)
        self.assertTrue(CoachAvailability.objects.filter(pk=canceled.pk).exists())

    def test_confirmed_rain_and_formal_other_cancellation_are_excluded(self):
        for offset, status, cancellation_type in ((0, "refunded", "rain"), (2, "refund_pending", "other")):
            self.start += timedelta(hours=offset)
            self.end += timedelta(hours=offset)
            canceled, _reservation = self._canceled_overlap(status=status, cancellation_type=cancellation_type)
            registration, created = self.register_private(key=f"after-{cancellation_type}")
            self.assertTrue(created)
            self.assertNotEqual(registration.availability_id, canceled.pk)

    def test_held_and_ambiguous_overlaps_remain_rejected(self):
        existing = CoachAvailability.objects.create(
            coach=self.coach, court=self.court, lesson_type=Reservation.LESSON_PRIVATE,
            start_at=self.start, end_at=self.end, capacity=1, target_level=User.LEVEL_ALL,
        )
        with self.assertRaises(ValidationError):
            self.register_private(key="ambiguous-overlap")
        existing.delete()
        _held, held_reservation = self._canceled_overlap(status="held")
        Reservation.objects.filter(pk=held_reservation.pk).update(status=Reservation.STATUS_ACTIVE)
        with self.assertRaises(ValidationError):
            self.register_private(key="held-overlap")


class CompletedLessonUnifiedViewTests(TestCase):
    def setUp(self):
        self.coach = User.objects.create_user(
            username="line_coach", full_name="表示コーチ", role=User.ROLE_COACH,
            password="password",
        )
        self.member = User.objects.create_user(
            username="line_027b63632d75", full_name="表示会員", role=User.ROLE_MEMBER,
        )
        self.court = Court.objects.create(name="統合画面コート", available_court_count=2)
        self.client.force_login(self.coach)

    def test_calendar_date_prefill_mode_labels_hour_choices_and_safe_member_name(self):
        response = self.client.get(reverse("club:completed_lesson_register"), {"date": "2026-09-06"})
        self.assertContains(response, 'value="2026-09-06"')
        self.assertContains(response, "実施済みとして登録")
        self.assertContains(response, 'value="09:00"')
        self.assertContains(response, 'value="21:00"')
        self.assertContains(response, "表示会員")
        self.assertNotContains(response, "line_027b63632d75")
        self.assertContains(response, "登録前サマリー")
        self.assertContains(response, "標準:")
        self.assertContains(response, "Math.min")
        self.assertContains(response, "+2")

    def test_member_candidates_use_normalized_display_name_order_and_exclude_coaches(self):
        User.objects.create_user(username="member-kata", full_name="カナ", role=User.ROLE_MEMBER)
        User.objects.create_user(username="member-hira", full_name="あい", role=User.ROLE_MEMBER)
        fixed_now = timezone.make_aware(datetime(2026, 9, 10, 12, 0))
        with (
            patch("club.completed_lesson_views.timezone.localdate", return_value=fixed_now.date()),
            patch("club.completed_lesson_views.timezone.now", return_value=fixed_now),
        ):
            response = self.client.get(reverse("club:completed_lesson_register"))
        self.assertEqual(response.status_code, 200)
        options = response.context["member_options"]
        labels = [row["label"] for row in options]
        self.assertLess(labels.index("あい"), labels.index("カナ"))
        self.assertNotIn("表示コーチ", labels)
        self.assertNotIn("line_027b63632d75", response.content.decode())

    def test_post_mode_change_does_not_save(self):
        response = self.client.post(reverse("club:completed_lesson_register"), {
            "date": "2026-09-06", "start_time": "17:00", "end_time": "19:00",
            "displayed_mode": "scheduled", "idempotency_key": "mode-change",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "登録区分が変わりました")
        self.assertFalse(CompletedLessonRegistration.objects.exists())

    def test_future_post_redirects_to_canonical_availability_form_without_saving(self):
        response = self.client.post(reverse("club:completed_lesson_register"), {
            "date": "2099-09-06", "start_time": "09:00", "end_time": "11:00",
            "displayed_mode": "scheduled", "lesson_type": Reservation.LESSON_GENERAL,
            "coach": self.coach.pk, "court": self.court.pk, "capacity": "5",
            "note": "", "idempotency_key": "future",
        })
        self.assertRedirects(
            response,
            f"{reverse('club:coach_availability_create')}?date=2099-09-06&source=calendar",
        )
        self.assertFalse(CoachAvailability.objects.exists())
        self.assertFalse(CompletedLessonRegistration.objects.exists())
