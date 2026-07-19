from datetime import date, datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from . import lesson_execution
from .models import (
    CoachAvailability,
    CoachExpense,
    Court,
    FixedLesson,
    LessonWaitlist,
    LessonWaitlistParticipant,
    Reservation,
    TicketLedger,
)


class ReservationFlowSmokeTests(TestCase):
    """
    予約導線の最低限の自動デバッグ用テストです。

    目的：
    - lesson-calendar が 500 にならないこと
    - 予約前確認URLが解決できること
    - 会員が通常レッスンを予約できること
    - 業務委託コーチが他コーチ担当レッスンを受講予約できること
    - 業務委託コーチが自分担当レッスンを予約できないこと
    - 2026年7月プレオープンはチケットを消費しないこと
    - 満員時にキャンセル待ち登録できること
    """

    def setUp(self):
        self.User = get_user_model()
        self.client = Client()

        self.court = Court.objects.create(
            name="テストコート",
            is_active=True,
            court_type=Court.COURT_SONO,
        )

        self.member = self._create_user(
            username="member_test",
            role=self.User.ROLE_MEMBER,
            full_name="会員 テスト",
            ticket_balance=0,
        )

        self.coach = self._create_user(
            username="coach_test",
            role=self.User.ROLE_COACH,
            full_name="飯塚 コーチ",
            ticket_balance=0,
        )

        self.contractor = self._create_user(
            username="contractor_test",
            role=self.User.ROLE_CONTRACTOR_COACH,
            full_name="業務委託 コーチ",
            ticket_balance=0,
        )

        self.lesson_date = timezone.localdate() + timedelta(days=7)

    def _create_user(self, *, username, role, full_name, ticket_balance=0):
        user = self.User.objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password="password12345",
        )
        user.role = role
        user.full_name = full_name
        user.phone_number = "09000000000"
        user.is_profile_completed = True
        user.member_level = self.User.LEVEL_BEGINNER
        user.ticket_balance = ticket_balance
        user.save()
        return user

    def _create_fixed_lesson(self, *, coach=None, lesson_date=None, title="テスト一般レッスン"):
        target_date = lesson_date or self.lesson_date
        return FixedLesson.objects.create(
            title=title,
            coach=coach or self.coach,
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            target_level_2="",
            start_date=target_date,
            weekday=target_date.weekday(),
            start_hour=19,
            capacity=6,
            coach_count=1,
            court_count=1,
            weeks_ahead=1,
            is_active=True,
        )

    def _post_lesson_calendar_reserve(self, *, user, fixed_lesson, lesson_date=None, action="reserve"):
        self.client.force_login(user)
        target_date = lesson_date or self.lesson_date
        return self.client.post(
            reverse("club:lesson_calendar"),
            data={
                "action": action,
                "fixed_lesson_id": str(fixed_lesson.pk),
                "lesson_date": target_date.isoformat(),
                "year": str(target_date.year),
                "month": str(target_date.month),
            },
        )

    def test_private_operation_endpoints_require_login(self):
        endpoints = (
            ("get", reverse("club:stringing_order_detail", args=[999999])),
            ("post", reverse("club:reservation_cancel", args=[999999])),
            ("get", reverse("club:coach_availability_list")),
        )
        for method, url in endpoints:
            with self.subTest(url=url):
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith(reverse("club:login")))

    def test_lesson_calendar_page_does_not_return_500_for_member(self):
        self._create_fixed_lesson()
        self.client.force_login(self.member)

        response = self.client.get(
            reverse("club:lesson_calendar"),
            data={"year": "2026", "month": "7"},
        )

        self.assertEqual(response.status_code, 200)

    def test_lesson_reservation_confirm_url_is_available(self):
        fixed_lesson = self._create_fixed_lesson()
        self.client.force_login(self.member)

        url = reverse("club:lesson_reservation_confirm")
        response = self.client.get(
            url,
            data={
                "fixed_lesson_id": str(fixed_lesson.pk),
                "lesson_date": self.lesson_date.isoformat(),
                "year": "2026",
                "month": "7",
            },
        )

        self.assertNotEqual(response.status_code, 500)

    def test_lesson_execution_creates_one_canonical_availability(self):
        fixed_lesson = self._create_fixed_lesson()
        fixed_lesson.court = None
        fixed_lesson.save(update_fields=["court"])
        start_at, end_at = fixed_lesson._build_datetimes_for_date(
            self.lesson_date,
        )

        first = lesson_execution._canonical_availability_for_fixed(
            fixed_lesson,
            start_at,
            end_at,
        )
        second = lesson_execution._canonical_availability_for_fixed(
            fixed_lesson,
            start_at,
            end_at,
        )

        self.assertIsNotNone(first)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.court_id, self.court.pk)
        self.assertEqual(first.capacity, fixed_lesson.effective_capacity())
        self.assertEqual(
            CoachAvailability.objects.filter(
                coach=self.coach,
                court=self.court,
                lesson_type=fixed_lesson.lesson_type,
                start_at=start_at,
                end_at=end_at,
            ).count(),
            1,
        )

    def test_member_can_reserve_regular_preopen_lesson_without_ticket_consumption(self):
        preopen_date = date(2026, 7, 3)
        fixed_lesson = self._create_fixed_lesson(lesson_date=preopen_date)
        mocked_now = timezone.make_aware(datetime(2026, 7, 2, 12, 0))

        with patch("django.utils.timezone.now", return_value=mocked_now):
            response = self._post_lesson_calendar_reserve(
                user=self.member,
                fixed_lesson=fixed_lesson,
                lesson_date=preopen_date,
            )

        self.assertEqual(response.status_code, 302)

        reservation = Reservation.objects.get(user=self.member, fixed_lesson=fixed_lesson)
        self.assertEqual(reservation.status, Reservation.STATUS_ACTIVE)
        self.assertEqual(reservation.tickets_used, 0)

        self.member.refresh_from_db()
        self.assertEqual(self.member.ticket_balance, 0)

    def test_preopen_level_exception_uses_actual_lesson_date(self):
        preopen_date = date(2026, 7, 3)
        fixed_lesson = self._create_fixed_lesson(lesson_date=preopen_date)
        fixed_lesson.target_level = self.User.LEVEL_ADVANCED
        fixed_lesson.save(update_fields=["target_level"])
        mocked_now = timezone.make_aware(datetime(2026, 7, 2, 12, 0))

        with patch("django.utils.timezone.now", return_value=mocked_now):
            response = self._post_lesson_calendar_reserve(
                user=self.member,
                fixed_lesson=fixed_lesson,
                lesson_date=preopen_date,
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Reservation.objects.filter(user=self.member, fixed_lesson=fixed_lesson).exists())

    def test_preopen_query_parameters_cannot_bypass_level_for_august_lesson(self):
        lesson_date = date(2026, 8, 7)
        fixed_lesson = self._create_fixed_lesson(lesson_date=lesson_date)
        fixed_lesson.target_level = self.User.LEVEL_ADVANCED
        fixed_lesson.save(update_fields=["target_level"])
        mocked_now = timezone.make_aware(datetime(2026, 8, 6, 12, 0))
        self.client.force_login(self.member)

        with patch("django.utils.timezone.now", return_value=mocked_now):
            response = self.client.post(
                reverse("club:lesson_calendar"),
                data={
                    "action": "reserve",
                    "fixed_lesson_id": fixed_lesson.pk,
                    "lesson_date": lesson_date.isoformat(),
                    "year": "2026",
                    "month": "7",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Reservation.objects.filter(user=self.member, fixed_lesson=fixed_lesson).exists())

    def test_stale_duplicate_cancel_refunds_tickets_only_once(self):
        lesson_date = date(2026, 8, 7)
        self.member.ticket_balance = 10
        self.member.save(update_fields=["ticket_balance"])
        fixed_lesson = self._create_fixed_lesson(lesson_date=lesson_date, title="二重返却防止テスト")
        mocked_now = timezone.make_aware(datetime(2026, 8, 6, 12, 0))

        with patch("django.utils.timezone.now", return_value=mocked_now):
            self._post_lesson_calendar_reserve(
                user=self.member,
                fixed_lesson=fixed_lesson,
                lesson_date=lesson_date,
            )

        first_copy = Reservation.objects.get(user=self.member, fixed_lesson=fixed_lesson)
        stale_copy = Reservation.objects.get(pk=first_copy.pk)
        self.assertTrue(first_copy.cancel(created_by=self.member))
        self.assertFalse(stale_copy.cancel(created_by=self.member))

        self.member.refresh_from_db()
        self.assertEqual(self.member.ticket_balance, 10)
        self.assertEqual(
            TicketLedger.objects.filter(
                reservation=first_copy,
                reason=TicketLedger.REASON_CANCEL_REFUND,
            ).count(),
            1,
        )

    def test_contractor_coach_can_take_other_coach_lesson(self):
        fixed_lesson = self._create_fixed_lesson(
            coach=self.coach,
            title="他コーチ担当レッスン",
        )

        response = self._post_lesson_calendar_reserve(
            user=self.contractor,
            fixed_lesson=fixed_lesson,
        )

        self.assertEqual(response.status_code, 302)

        reservation = Reservation.objects.get(user=self.contractor, fixed_lesson=fixed_lesson)
        self.assertEqual(reservation.status, Reservation.STATUS_ACTIVE)
        self.assertEqual(reservation.coach_id, self.coach.pk)

    def test_contractor_coach_cannot_take_own_lesson(self):
        fixed_lesson = self._create_fixed_lesson(
            coach=self.contractor,
            title="自分担当レッスン",
        )

        response = self._post_lesson_calendar_reserve(
            user=self.contractor,
            fixed_lesson=fixed_lesson,
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            Reservation.objects.filter(
                user=self.contractor,
                fixed_lesson=fixed_lesson,
                status=Reservation.STATUS_ACTIVE,
            ).exists()
        )

    def test_full_lesson_can_accept_waitlist_from_member(self):
        fixed_lesson = self._create_fixed_lesson(title="満員テストレッスン")

        members = []
        for index in range(6):
            members.append(
                self._create_user(
                    username=f"full_member_{index}",
                    role=self.User.ROLE_MEMBER,
                    full_name=f"満員 会員{index}",
                    ticket_balance=0,
                )
            )

        fixed_lesson.members.set(members)
        fixed_lesson.sync_future_reservations(created_by=self.coach)

        response = self._post_lesson_calendar_reserve(
            user=self.member,
            fixed_lesson=fixed_lesson,
            action="join_waitlist",
        )

        self.assertEqual(response.status_code, 302)
        waitlist = LessonWaitlist.objects.filter(
                fixed_lesson=fixed_lesson,
                user=self.member,
                status=LessonWaitlist.STATUS_WAITING,
            ).get()
        snapshot = LessonWaitlistParticipant.objects.get(waitlist=waitlist)
        self.assertEqual(snapshot.parent, self.member)
        self.assertEqual(snapshot.participant_type, "self")
        self.assertEqual(snapshot.participant_name, self.member.display_name())

        second_response = self._post_lesson_calendar_reserve(
            user=self.member,
            fixed_lesson=fixed_lesson,
            action="join_waitlist",
        )
        self.assertEqual(second_response.status_code, 302)
        self.assertEqual(
            LessonWaitlist.objects.filter(
                fixed_lesson=fixed_lesson,
                user=self.member,
                status=LessonWaitlist.STATUS_WAITING,
            ).count(),
            1,
        )

    def test_lesson_calendar_get_does_not_resync_or_mutate_fixed_lessons(self):
        self._create_fixed_lesson(title="閲覧時同期禁止テスト")
        with patch.object(FixedLesson, "sync_future_reservations") as sync_mock:
            response = self.client.get(reverse("club:lesson_calendar"))

        self.assertEqual(response.status_code, 200)
        sync_mock.assert_not_called()

    def test_waitlist_promote_rejects_protocol_relative_next_url(self):
        fixed_lesson = self._create_fixed_lesson(title="安全な戻り先テスト")
        start_at, end_at = fixed_lesson._build_datetimes_for_date(self.lesson_date)
        waitlist = LessonWaitlist.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court,
            fixed_lesson=fixed_lesson,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=end_at,
            status=LessonWaitlist.STATUS_CANCELED,
        )
        self.client.force_login(self.coach)

        response = self.client.post(
            reverse("club:lesson_waitlist_promote", args=[waitlist.pk]),
            data={"next": "//attacker.example/redirect"},
        )

        self.assertRedirects(
            response,
            reverse("club:reservation_list"),
            fetch_redirect_response=False,
        )

    def test_waitlist_promote_is_idempotent(self):
        self.member.ticket_balance = 10
        self.member.save(update_fields=["ticket_balance"])
        fixed_lesson = self._create_fixed_lesson(title="二重繰り上げ防止テスト")
        fixed_lesson.sync_future_reservations(created_by=self.coach)
        start_at, end_at = fixed_lesson._build_datetimes_for_date(self.lesson_date)
        waitlist = LessonWaitlist.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court,
            fixed_lesson=fixed_lesson,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=end_at,
        )
        self.client.force_login(self.coach)
        url = reverse("club:lesson_waitlist_promote", args=[waitlist.pk])

        first_response = self.client.post(url)
        second_response = self.client.post(url)

        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(second_response.status_code, 302)
        self.assertEqual(
            Reservation.objects.filter(
                user=self.member,
                fixed_lesson=fixed_lesson,
                status=Reservation.STATUS_ACTIVE,
            ).count(),
            1,
        )
        waitlist.refresh_from_db()
        self.assertEqual(waitlist.status, LessonWaitlist.STATUS_CONVERTED)

    def test_unassigned_coach_cannot_manage_another_lesson_court_expense(self):
        start_at = (timezone.now() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=start_at + timedelta(hours=2),
            capacity=6,
            status=CoachAvailability.STATUS_OPEN,
        )
        self.client.force_login(self.contractor)

        response = self.client.get(
            reverse("club:coach_expense_manage"),
            data={"availability_id": availability.pk},
        )

        self.assertEqual(response.status_code, 403)

    def test_court_expense_payer_must_be_one_of_the_main_coaches(self):
        unrelated_coach = self._create_user(
            username="unrelated_coach",
            role=self.User.ROLE_COACH,
            full_name="無関係 コーチ",
        )
        start_at = (timezone.now() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=start_at + timedelta(hours=2),
            capacity=6,
        )
        self.client.force_login(self.coach)

        response = self.client.post(
            reverse("club:coach_expense_manage"),
            data={
                "action": "create_court_transfer",
                "availability_id": availability.pk,
                "payer_coach_id": unrelated_coach.pk,
                "amount": "3000",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(CoachExpense.objects.exists())

    def test_court_expense_can_credit_a_different_main_coach(self):
        payer = self._create_user(
            username="main_payer",
            role=self.User.ROLE_COACH,
            full_name="清水峻平",
        )
        lesson_date = timezone.localdate() + timedelta(days=2)
        fixed_lesson = self._create_fixed_lesson(lesson_date=lesson_date, title="支払者振替テスト")
        fixed_lesson.sync_future_reservations(created_by=self.coach)
        start_at, _end_at = fixed_lesson._build_datetimes_for_date(lesson_date)
        availability = CoachAvailability.objects.get(
            coach=self.coach,
            court=self.court,
            start_at=start_at,
        )
        self.client.force_login(self.coach)

        calendar_response = self.client.get(
            reverse("club:lesson_calendar"),
            data={"year": start_at.year, "month": start_at.month},
        )
        expense_url = (
            f"{reverse('club:coach_expense_manage')}?"
            f"availability_id={availability.pk}&amp;date={start_at.date().isoformat()}"
        )
        self.assertContains(calendar_response, expense_url)

        response = self.client.post(
            reverse("club:coach_expense_manage"),
            data={
                "action": "create_court_transfer",
                "availability_id": availability.pk,
                "payer_coach_id": payer.pk,
                "amount": "3000",
            },
        )

        self.assertEqual(response.status_code, 302)
        expense = CoachExpense.objects.get()
        self.assertEqual(expense.created_by, payer)
        self.assertGreater(len(expense.note), 255)

        repeated_response = self.client.post(
            reverse("club:coach_expense_manage"),
            data={
                "action": "create_court_transfer",
                "availability_id": availability.pk,
                "payer_coach_id": payer.pk,
                "amount": "3000",
            },
        )
        self.assertEqual(repeated_response.status_code, 302)
        self.assertEqual(CoachExpense.objects.count(), 1)

        from .court_expense_transfer import _parse_note
        from .settlement_balance_policy import _court_transfer_allocation

        allocation = _court_transfer_allocation(
            [{"expense": expense, "amount": expense.amount, "meta": _parse_note(expense.note)}],
            [self.coach.pk, payer.pk],
        )
        self.assertEqual(allocation["burden_by_coach"], {self.coach.pk: 3000})
        self.assertEqual(allocation["reimbursement_by_coach"], {payer.pk: 3000})

    def test_direct_reservation_rejects_fixed_lesson_at_capacity(self):
        fixed_lesson = self._create_fixed_lesson(title="直接保存満員テスト")
        members = [
            self._create_user(
                username=f"capacity_member_{index}",
                role=self.User.ROLE_MEMBER,
                full_name=f"定員 会員{index}",
            )
            for index in range(6)
        ]
        fixed_lesson.members.set(members)
        start_at, end_at = fixed_lesson._build_datetimes_for_date(
            self.lesson_date,
        )
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=end_at,
            capacity=6,
            status=CoachAvailability.STATUS_OPEN,
        )

        with self.assertRaisesMessage(ValidationError, "このレッスンは満員です"):
            Reservation.objects.create(
                user=self.member,
                coach=self.coach,
                court=self.court,
                availability=availability,
                fixed_lesson=fixed_lesson,
                lesson_type=Reservation.LESSON_GENERAL,
                target_level=self.User.LEVEL_BEGINNER,
                start_at=start_at,
                end_at=end_at,
                status=Reservation.STATUS_ACTIVE,
            )

    def test_court_number_notice_uses_selected_calendar_lesson_without_dropdown(self):
        lesson_date = timezone.localdate() + timedelta(days=1)
        fixed_lesson = self._create_fixed_lesson(
            lesson_date=lesson_date,
            title="選択対象レッスン",
        )
        start_at, end_at = fixed_lesson._build_datetimes_for_date(lesson_date)
        availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=end_at,
            capacity=6,
            status=CoachAvailability.STATUS_OPEN,
        )
        first_member = self._create_user(
            username="line_first",
            role=self.User.ROLE_MEMBER,
            full_name="後藤 会員",
        )
        selected_member = self._create_user(
            username="line_selected",
            role=self.User.ROLE_MEMBER,
            full_name="阿部 会員",
        )
        other_lesson_member = self._create_user(
            username="line_other_lesson",
            role=self.User.ROLE_MEMBER,
            full_name="別枠 会員",
        )
        other_fixed_lesson = self._create_fixed_lesson(
            lesson_date=lesson_date,
            title="同時刻の別レッスン",
        )

        def create_reservation(user, target_fixed_lesson):
            return Reservation.objects.create(
                user=user,
                coach=self.coach,
                court=self.court,
                availability=availability,
                fixed_lesson=target_fixed_lesson,
                lesson_type=Reservation.LESSON_GENERAL,
                target_level=self.User.LEVEL_BEGINNER,
                start_at=start_at,
                end_at=end_at,
                status=Reservation.STATUS_ACTIVE,
            )

        create_reservation(first_member, fixed_lesson)
        selected_reservation = create_reservation(selected_member, fixed_lesson)
        create_reservation(other_lesson_member, other_fixed_lesson)

        self.client.force_login(self.coach)
        response = self.client.get(
            reverse("club:court_number_line_notice"),
            data={"slot_id": selected_reservation.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_slot"].pk, selected_reservation.pk)
        self.assertContains(
            response,
            f'name="slot_id" value="{selected_reservation.pk}"',
        )
        self.assertNotContains(response, '<select id="slot_id"')
        self.assertContains(response, "後藤 会員")
        self.assertContains(response, "阿部 会員")
        self.assertNotContains(response, "別枠 会員")

        cache.clear()
        send_data = {
            "slot_id": selected_reservation.pk,
            "court_number": "3コート",
            "note": "テスト連絡",
            "confirm_send": "yes",
            "action": "send",
        }
        with patch(
            "club.court_number_line_notice.notify_user_line_only",
            side_effect=[
                {"line": False, "email": False},
                {"line": True, "email": False},
            ],
        ) as line_notify_mock:
            with patch(
                "club.court_number_line_notice.notify_user_email_only",
                return_value={"line": False, "email": True},
            ) as email_notify_mock:
                first_send = self.client.post(
                    reverse("club:court_number_line_notice"),
                    data=send_data,
                )
                duplicate_send = self.client.post(
                    reverse("club:court_number_line_notice"),
                    data=send_data,
                )

        self.assertEqual(first_send.status_code, 302)
        self.assertEqual(duplicate_send.status_code, 302)
        self.assertEqual(line_notify_mock.call_count, 2)
        self.assertEqual(email_notify_mock.call_count, 1)
