from datetime import date, datetime, timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.models import CoachAvailability, Court, CourtBookingInfo, User


class MultiCourtBookingInfoTests(TestCase):
    def setUp(self):
        self.coach1 = User.objects.create_user(username="multi-court-1", role=User.ROLE_COACH)
        self.coach2 = User.objects.create_user(username="multi-court-2", role=User.ROLE_COACH)
        self.staff = User.objects.create_superuser(username="multi-court-staff", password="password")
        self.court = Court.objects.create(name="複数面テスト", available_court_count=4)
        start = timezone.make_aware(datetime.combine(date(2026, 11, 8), datetime.min.time()).replace(hour=17))
        self.availability = CoachAvailability.objects.create(
            coach=self.coach1,
            coach_2=self.coach2,
            court=self.court,
            start_at=start,
            end_at=start + timedelta(hours=2),
            lesson_type=CoachAvailability.LESSON_GENERAL,
            capacity=10,
        )
        self.info1 = CourtBookingInfo.objects.create(
            availability=self.availability,
            position=1,
            reservation_number="0123456789",
            booking_account_kind="coach",
            booking_account_coach=self.coach1,
        )
        self.info2 = CourtBookingInfo.objects.create(
            availability=self.availability,
            position=2,
            reservation_number="9876543210",
            booking_account_kind="coach",
            booking_account_coach=self.coach2,
        )

    def _edit_data(self, *, delete_id=""):
        return {
            "coach": self.coach1.pk,
            "coach_2": "",
            "substitute_coach": "",
            "court": self.court.pk,
            "lesson_type": CoachAvailability.LESSON_GENERAL,
            "court_payer_kind": "",
            "court_payer_coach": "",
            "target_level": User.LEVEL_BEGINNER,
            "target_level_2": "",
            "coach_count": 1,
            "court_count": 1,
            "capacity": 5,
            "custom_ticket_price": 0,
            "custom_duration_hours": 0,
            "note": "",
            "start_date": "2026-11-08",
            "start_hour": "17",
            "end_date": "2026-11-08",
            "end_hour": "19",
            "delete_court_booking_info": delete_id,
        }

    def test_reservation_number_server_validation(self):
        for value in ("123456789", "12345678901", "12345abcde"):
            with self.subTest(value=value):
                info = CourtBookingInfo(availability=self.availability, position=3, reservation_number=value)
                with self.assertRaisesMessage(ValidationError, "予約番号は10桁の数字で入力してください。"):
                    info.full_clean()

    def test_two_to_one_requires_explicit_choice_without_deleting(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("club:coach_availability_edit", args=[self.availability.pk]),
            self._edit_data(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "利用コートが2面から1面になります。削除するコート情報を選択してください。")
        self.availability.refresh_from_db()
        self.assertEqual(self.availability.court_count, 2)
        self.assertEqual(self.availability.court_booking_infos.count(), 2)

    def test_two_to_one_deletes_selected_and_normalizes_remaining_position(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("club:coach_availability_edit", args=[self.availability.pk]),
            self._edit_data(delete_id=self.info1.pk),
        )
        self.assertEqual(response.status_code, 302)
        self.availability.refresh_from_db()
        self.assertEqual(self.availability.court_count, 1)
        self.assertFalse(CourtBookingInfo.objects.filter(pk=self.info1.pk).exists())
        remaining = self.availability.court_booking_infos.get()
        self.assertEqual(remaining.position, 1)
        self.assertEqual(remaining.reservation_number, "9876543210")
        self.assertEqual(remaining.booking_account_coach, self.coach2)

    def test_calendar_payment_button_permission_and_month(self):
        self.client.force_login(self.coach1)
        response = self.client.get(reverse("club:lesson_calendar"), {"year": 2026, "month": 11})
        self.assertContains(response, "コート支払一覧")
        self.assertContains(response, "court-payment-list/?year=2026&amp;month=11")

        member = User.objects.create_user(username="multi-court-member", role=User.ROLE_MEMBER)
        self.client.force_login(member)
        self.assertNotContains(
            self.client.get(reverse("club:lesson_calendar"), {"year": 2026, "month": 11}),
            "コート支払一覧",
        )
