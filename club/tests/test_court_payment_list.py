from datetime import datetime, timedelta
from io import BytesIO

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from pypdf import PdfReader

from club import lesson_execution
from club.court_payment_list import payment_rows
from club.lesson_execution_storage import save_status
from club.models import CoachAvailability, Court
from club.settlement_models import MonthlySettlement


class CourtPaymentListTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.coach = User.objects.create_user(
            username="payment-coach",
            password="password12345",
            role=User.ROLE_COACH,
            full_name="飯塚 研太朗",
        )
        self.other_coach = User.objects.create_user(
            username="booking-coach",
            password="password12345",
            role=User.ROLE_COACH,
            full_name="清水 峻平",
        )
        self.member = User.objects.create_user(
            username="payment-member",
            password="password12345",
            role=User.ROLE_MEMBER,
        )
        self.sono = Court.objects.create(
            name="西猪名公園", court_type=Court.COURT_SONO, is_active=True
        )
        self.other_court = Court.objects.create(
            name="尼崎記念公園", court_type=Court.COURT_AMAGASAKI, is_active=True
        )

    def _availability(
        self,
        day,
        hour,
        *,
        court=None,
        number="0123456789",
        account_kind="coach",
        account_coach=None,
        account_other="",
        coach=None,
    ):
        start = timezone.make_aware(datetime(2026, 11, day, hour))
        return CoachAvailability.objects.create(
            coach=coach or self.coach,
            court=court or self.sono,
            court_reservation_number=number,
            court_booking_account_kind=account_kind,
            court_booking_account_coach=(
                self.other_coach if account_coach is None and account_kind == "coach" else account_coach
            ),
            court_booking_account_other=account_other,
            start_at=start,
            end_at=start + timedelta(hours=2),
            capacity=4,
            status=CoachAvailability.STATUS_OPEN,
        )

    def _cancel(self, availability, cancellation_type):
        settlement, _ = MonthlySettlement.objects.get_or_create(year=2026, month=11)
        save_status(
            settlement,
            lesson_execution._availability_key(availability),
            lesson_execution.STATUS_RAIN_CANCELED,
            self.coach,
            cancellation_type=cancellation_type,
        )

    def _get(self, name="club:court_payment_list", **extra):
        self.client.force_login(self.coach)
        return self.client.get(
            reverse(name), {"year": 2026, "month": 11}, **extra
        )

    def _pdf_text(self, response):
        return "\n".join(
            page.extract_text() or "" for page in PdfReader(BytesIO(response.content)).pages
        )

    def test_rows_are_sorted_by_date_start_time_and_availability_id(self):
        late_day = self._availability(3, 19, number="3000000000")
        same_time_first = self._availability(1, 19, number="1000000001")
        early = self._availability(1, 9, number="1000000000")
        same_time_second = self._availability(
            1, 19, number="1000000002", coach=self.other_coach
        )

        rows = payment_rows(2026, 11)

        self.assertEqual(
            [row["availability_id"] for row in rows],
            [early.pk, same_time_first.pk, same_time_second.pk, late_day.pk],
        )

    def test_only_sono_is_shown_without_court_name_or_fee(self):
        self._availability(1, 19, number="0123456789")
        self._availability(2, 19, court=self.other_court, number="9999999999")

        response = self._get()

        self.assertContains(response, "0123456789")
        self.assertNotContains(response, "9999999999")
        self.assertNotContains(response, "コート名")
        self.assertNotContains(response, "コート代")
        self.assertEqual(response.content.decode().count("西猪名公園"), 1)

    def test_reservation_number_preserves_leading_zero_and_missing_is_shown(self):
        self._availability(1, 19, number="0123456789")
        self._availability(2, 19, number="")

        response = self._get()

        self.assertContains(response, "予約番号：0123456789")
        self.assertContains(response, "予約番号：未設定")
        self.assertContains(response, 'court-payment-number missing')

    def test_booking_account_uses_existing_coach_other_and_unset_display(self):
        self._availability(1, 19)
        self._availability(
            2, 19, account_kind="other", account_coach=None, account_other="公園共通アカウント"
        )
        self._availability(3, 19, account_kind="", account_coach=None)

        response = self._get()

        self.assertContains(response, "予約者：清水 峻平")
        self.assertContains(response, "予約者：公園共通アカウント")
        self.assertContains(response, "予約者：未設定")

    def test_rain_and_other_cancellations_are_excluded_but_future_is_shown(self):
        rain = self._availability(1, 19, number="1111111111")
        other = self._availability(2, 19, number="2222222222")
        future = self._availability(30, 19, number="3333333333")
        self._cancel(rain, lesson_execution.CANCELLATION_TYPE_RAIN)
        self._cancel(other, lesson_execution.CANCELLATION_TYPE_OTHER)

        response = self._get()

        self.assertNotContains(response, "1111111111")
        self.assertNotContains(response, "2222222222")
        self.assertContains(response, "3333333333")

    def test_pdf_has_same_order_and_only_required_fields(self):
        self._availability(3, 19, number="3000000000")
        self._availability(1, 9, number="1000000000")

        html = self._get()
        pdf = self._get("club:court_payment_list_pdf")
        text = self._pdf_text(pdf)

        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertLess(html.content.index(b"1000000000"), html.content.index(b"3000000000"))
        self.assertLess(text.index("1000000000"), text.index("3000000000"))
        for expected in ("西猪名公園 コート支払一覧", "2026年11月", "予約番号", "予約者"):
            self.assertIn(expected, text)
        for hidden in ("コート名", "コート代", "レッスン種別", "参加者", "担当コーチ"):
            self.assertNotIn(hidden, text)

    def test_member_cannot_access_html_or_pdf(self):
        self.client.force_login(self.member)
        for name in ("club:court_payment_list", "club:court_payment_list_pdf"):
            response = self.client.get(reverse(name), {"year": 2026, "month": 11})
            self.assertEqual(response.status_code, 403)

    def test_html_and_pdf_are_read_only(self):
        self._availability(1, 19)
        self.client.force_login(self.coach)

        with CaptureQueriesContext(connection) as captured:
            self.client.get(reverse("club:court_payment_list"), {"year": 2026, "month": 11})
            self.client.get(reverse("club:court_payment_list_pdf"), {"year": 2026, "month": 11})

        write_sql = [
            query["sql"] for query in captured.captured_queries
            if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        self.assertEqual(write_sql, [])

    def test_pdf_url_is_absolute_normal_web_url_and_line_adds_external_flag(self):
        normal = self._get()
        line = self._get(
            HTTP_USER_AGENT="Mozilla/5.0 Line/14.0",
        )

        normal_url = normal.context["pdf_url"]
        line_url = line.context["pdf_url"]
        self.assertTrue(normal_url.startswith("http://testserver/coach/"))
        self.assertNotIn("openExternalBrowser", normal_url)
        self.assertIn("openExternalBrowser=1", line_url)
        self.assertNotIn("/liff/", line_url)

    def test_month_navigation_and_calendar_return_are_present(self):
        response = self._get()

        self.assertContains(response, "対象月：2026年11月")
        self.assertContains(response, "year=2026&amp;month=10")
        self.assertContains(response, "year=2026&amp;month=12")
        self.assertContains(response, reverse("club:lesson_calendar"))
