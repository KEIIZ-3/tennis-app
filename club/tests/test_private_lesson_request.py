from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch

from club.forms import ReservationCreateForm
from club.models import Court, Reservation


class PrivateLessonRequestTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.member = User.objects.create_user(
            username="private-member",
            password="password12345",
            role=User.ROLE_MEMBER,
            full_name="予約会員",
            member_level=User.LEVEL_BEGINNER,
            email="member@example.com",
            phone_number="09000000000",
            is_profile_completed=True,
            ticket_balance=0,
        )
        self.coach = User.objects.create_user(
            username="private-coach",
            password="password12345",
            role=User.ROLE_COACH,
            full_name="担当コーチ",
            member_level=User.LEVEL_BEGINNER,
        )
        self.other_coach = User.objects.create_user(
            username="other-coach",
            password="password12345",
            role=User.ROLE_COACH,
            full_name="別コーチ",
            member_level=User.LEVEL_BEGINNER,
        )
        self.court = Court.objects.create(
            name="内部割当用コート",
            is_active=True,
            court_type=Court.COURT_SONO,
        )
        lesson_date = timezone.localdate() + timedelta(days=5)
        self.start_at = timezone.make_aware(datetime.combine(lesson_date, time(18)))
        self.end_at = self.start_at + timedelta(hours=2)

    def _pending(self, *, status=Reservation.STATUS_PENDING, start_at=None, end_at=None):
        return Reservation.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=self.member.member_level,
            requested_court_type=Court.COURT_OTHER,
            requested_court_note="未登録市民テニスコート2番",
            start_at=start_at or self.start_at,
            end_at=end_at or self.end_at,
            status=status,
        )

    def test_calendar_has_private_only_request_link_and_form(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("club:lesson_calendar"))
        self.assertContains(response, "プライベートレッスンを依頼")
        self.assertContains(response, "private_only=1")

        response = self.client.get(
            reverse("club:reservation_create"),
            {"private_only": "1", "lesson_type": Reservation.LESSON_PRIVATE},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["private_only"])
        self.assertTrue(response.context["form"].fields["coach_choice"].required)
        self.assertIn("requested_court_note", response.context["form"].fields)
        self.assertNotIn("end_date", response.context["form"].fields)
        request_form_html = response.content.decode().split(
            '<form method="post" id="reservation-request-form">', 1
        )[1].split("</form>", 1)[0]
        self.assertEqual(request_form_html.count('type="submit"'), 1)
        self.assertIn("申請を送信する", request_form_html)
        self.assertNotIn("予約確認へ", request_form_html)
        self.assertNotIn(f'href="{reverse("club:reservation_list")}"', request_form_html)
        self.assertNotIn("reserve-mobile-actions", request_form_html)
        self.assertContains(response, 'startHour.addEventListener("change", setDefaultEndHour)')
        self.assertContains(response, "Number.parseInt(startHour.value, 10) + 1")

    def test_regular_request_form_keeps_mobile_confirmation_action(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("club:reservation_create"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["private_only"])
        self.assertContains(response, "予約確認へ")
        self.assertContains(response, "reserve-mobile-actions")
        self.assertContains(response, ">確認</a>")

    def test_private_form_accepts_free_court_and_calculates_tickets(self):
        lesson_date = self.start_at.date()
        form = ReservationCreateForm(
            data={
                "lesson_type": Reservation.LESSON_PRIVATE,
                "coach_choice": str(self.coach.pk),
                "requested_court_note": "Courtマスタにない公園コート",
                "start_date": lesson_date.isoformat(),
                "start_hour": "18",
                "end_hour": "21",
            },
            request_user=self.member,
            private_only=True,
        )
        self.assertTrue(form.is_valid(), form.errors)
        reservation = form.save(commit=False)
        self.assertEqual(reservation.calculate_tickets_used(), 6)
        self.assertEqual(reservation.start_at.date(), reservation.end_at.date())

    def test_private_form_uses_same_day_and_preserves_manually_selected_end_hour(self):
        lesson_date = self.start_at.date()
        form = ReservationCreateForm(
            data={
                "lesson_type": Reservation.LESSON_PRIVATE,
                "coach_choice": str(self.coach.pk),
                "requested_court_note": "自由入力コート",
                "start_date": lesson_date.isoformat(),
                "start_hour": "13",
                "end_hour": "15",
            },
            request_user=self.member,
            private_only=True,
        )
        self.assertTrue(form.is_valid(), form.errors)
        reservation = form.save(commit=False)
        self.assertEqual(timezone.localtime(reservation.end_at).hour, 15)
        self.assertEqual(reservation.calculate_tickets_used(), 4)

    def test_private_form_one_hour_uses_two_tickets(self):
        lesson_date = self.start_at.date()
        form = ReservationCreateForm(
            data={
                "lesson_type": Reservation.LESSON_PRIVATE,
                "coach_choice": str(self.coach.pk),
                "requested_court_note": "自由入力コート",
                "start_date": lesson_date.isoformat(),
                "start_hour": "13",
                "end_hour": "14",
            },
            request_user=self.member,
            private_only=True,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save(commit=False).calculate_tickets_used(), 2)

    def test_regular_request_form_still_requires_end_date(self):
        form = ReservationCreateForm(request_user=self.member)
        self.assertIn("end_date", form.fields)

    def test_private_form_ignores_posted_end_date_and_prevents_day_crossing(self):
        lesson_date = self.start_at.date()
        form = ReservationCreateForm(
            data={
                "lesson_type": Reservation.LESSON_PRIVATE,
                "coach_choice": str(self.coach.pk),
                "requested_court_note": "自由入力コート",
                "start_date": lesson_date.isoformat(),
                "start_hour": "20",
                "end_date": (lesson_date + timedelta(days=1)).isoformat(),
                "end_hour": "21",
            },
            request_user=self.member,
            private_only=True,
        )
        self.assertTrue(form.is_valid(), form.errors)
        reservation = form.save(commit=False)
        self.assertEqual(reservation.start_at.date(), reservation.end_at.date())

    def test_request_creates_pending_and_notifies_only_selected_coach(self):
        lesson_date = self.start_at.date()
        self.client.force_login(self.member)
        with patch("club.views._send_email_notification_safely") as send_mock:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("club:reservation_create"),
                    {
                        "private_only": "1",
                        "lesson_type": Reservation.LESSON_PRIVATE,
                        "coach_choice": str(self.coach.pk),
                        "requested_court_note": "自由入力の市民コート",
                        "start_date": lesson_date.isoformat(),
                        "start_hour": "18",
                        "end_hour": "20",
                    },
                )

        self.assertRedirects(
            response, reverse("club:reservation_list"), fetch_redirect_response=False
        )
        reservation = Reservation.objects.get(user=self.member)
        self.assertEqual(reservation.status, Reservation.STATUS_PENDING)
        self.assertEqual(reservation.lesson_type, Reservation.LESSON_PRIVATE)
        self.assertEqual(reservation.coach_id, self.coach.pk)
        self.assertEqual(reservation.requested_court_note, "自由入力の市民コート")
        self.assertEqual(reservation.tickets_used, 4)
        send_mock.assert_called_once()
        self.assertEqual(send_mock.call_args.args[0].pk, self.coach.pk)
        self.assertNotEqual(send_mock.call_args.args[0].pk, self.other_coach.pk)

    def test_only_active_private_is_calendar_event_without_private_details(self):
        pending = self._pending()
        active = self._pending(
            status=Reservation.STATUS_ACTIVE,
            start_at=self.start_at + timedelta(days=1),
            end_at=self.end_at + timedelta(days=1),
        )
        canceled = self._pending(
            status=Reservation.STATUS_CANCELED,
            start_at=self.start_at + timedelta(days=2),
            end_at=self.end_at + timedelta(days=2),
        )
        response = self.client.get(
            reverse("club:lesson_calendar"),
            {"year": self.start_at.year, "month": self.start_at.month},
        )
        private_rows = [row for row in response.context["schedule_rows"] if row.get("is_private_event")]
        self.assertEqual([row["id"] for row in private_rows], [f"private-{active.pk}"])
        self.assertFalse(private_rows[0]["can_book"])
        self.assertEqual(private_rows[0]["court_name"], "")
        content = response.content.decode()
        self.assertNotIn(self.member.full_name, content)
        self.assertNotIn(pending.requested_court_note, content)
        self.assertNotIn(f"private-{pending.pk}", content)
        self.assertNotIn(f"private-{canceled.pk}", content)

    def test_approval_uses_existing_ticket_flow_with_zero_balance(self):
        reservation = self._pending()
        reservation.activate_after_approval(created_by=self.coach)
        reservation.refresh_from_db()
        self.member.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.STATUS_ACTIVE)
        self.assertEqual(reservation.tickets_used, 4)
        self.assertEqual(self.member.ticket_balance, -4)

    def test_active_private_blocks_overlapping_reservation_for_same_coach(self):
        self._pending(status=Reservation.STATUS_ACTIVE)
        overlap = Reservation(
            user=get_user_model().objects.create_user(
                username="overlap-member",
                password="password12345",
                role=get_user_model().ROLE_MEMBER,
                member_level=get_user_model().LEVEL_BEGINNER,
            ),
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_PRIVATE,
            target_level=get_user_model().LEVEL_BEGINNER,
            requested_court_note="別会場",
            start_at=self.start_at + timedelta(hours=1),
            end_at=self.end_at + timedelta(hours=1),
            status=Reservation.STATUS_ACTIVE,
        )
        with self.assertRaisesMessage(ValidationError, "担当コーチには同じ時間帯の予約があります。"):
            overlap.full_clean()
