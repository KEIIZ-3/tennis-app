from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.forms import StringingOrderRecordForm
from club.models import StringingOrder
from club.settlement_service import calculate_monthly_settlement
from club.stringing_service import (
    create_stringing_order,
    create_recorded_stringing_order,
    recognized_stringing_orders,
    stringing_revenue_amount,
    update_stringing_order_status,
)


class StringingServiceTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.member = user_model.objects.create_user(
            username="stringing-member",
            role=user_model.ROLE_MEMBER,
        )
        self.iizuka = user_model.objects.create_user(
            username="stringing-iizuka",
            full_name="飯塚　研太朗",
            role=user_model.ROLE_COACH,
        )
        self.shimizu = user_model.objects.create_user(
            username=StringingOrder.DEFAULT_ASSIGNED_COACH_USERNAME,
            email=StringingOrder.DEFAULT_ASSIGNED_COACH_EMAIL,
            full_name="清水 峻平",
            role=user_model.ROLE_COACH,
        )
        self.inoue = user_model.objects.create_user(
            username="stringing-inoue",
            full_name="井上春佳",
            role=user_model.ROLE_COACH,
        )
        self.contractor = user_model.objects.create_user(
            username="stringing-contractor",
            full_name="業務委託コーチ",
            role=user_model.ROLE_CONTRACTOR_COACH,
        )
        self.inactive_iizuka = user_model.objects.create_user(
            username="inactive-stringing-iizuka",
            full_name="飯塚研太朗",
            role=user_model.ROLE_COACH,
            is_active=False,
        )

    def _order(self, **overrides):
        values = {
            "preferred_delivery_time": "2026-08-20",
            "delivery_requested": False,
        }
        values.update(overrides)
        return StringingOrder(**values)

    def test_create_assigns_default_coach_and_fixed_prices(self):
        order = create_stringing_order(order=self._order(), user=self.member)

        self.assertEqual(order.assigned_coach, self.shimizu)
        self.assertEqual(order.status, StringingOrder.STATUS_REQUESTED)
        self.assertEqual(order.base_price, 1200)
        self.assertEqual(order.delivery_fee, 0)
        self.assertEqual(order.total_price(), 1200)

    def test_supported_coaches_are_allowed_but_inoue_and_contractor_are_rejected(self):
        for coach in (self.iizuka, self.shimizu):
            order = create_stringing_order(
                order=self._order(assigned_coach=coach), user=self.member
            )
            self.assertEqual(order.assigned_coach, coach)

        for coach in (self.inoue, self.contractor):
            with self.assertRaises(ValidationError):
                create_stringing_order(
                    order=self._order(assigned_coach=coach), user=self.member
                )

    def test_delivery_is_included_and_string_choice_does_not_change_fee(self):
        for string_name in ("", "持込ガット", "購入ガット"):
            order = create_stringing_order(
                order=self._order(
                    string_name=string_name,
                    delivery_requested=True,
                    delivery_location="テストコート",
                    preferred_delivery_time="2026-08-20 18:00",
                ),
                user=self.member,
            )
            self.assertEqual(order.base_price, 1200)
            self.assertEqual(order.delivery_fee, 500)
            self.assertEqual(order.total_price(), 1700)

    def test_recognition_uses_created_month_and_excludes_only_canceled(self):
        orders = []
        for status in (
            StringingOrder.STATUS_REQUESTED,
            StringingOrder.STATUS_IN_PROGRESS,
            StringingOrder.STATUS_COMPLETED,
            StringingOrder.STATUS_CANCELED,
        ):
            order = create_stringing_order(order=self._order(), user=self.member)
            order.status = status
            order.save(update_fields=["status"])
            orders.append(order)

        month_start = timezone.localdate().replace(day=1)
        next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        recognized = list(
            recognized_stringing_orders(
                StringingOrder.objects.all(),
                month_start=month_start,
                next_month=next_month,
            )
        )

        self.assertEqual({order.pk for order in recognized}, {order.pk for order in orders[:3]})
        self.assertEqual(stringing_revenue_amount(orders[3]), 0)

    def test_recalculation_is_idempotent_and_status_or_coach_change_replaces_amount(self):
        order = create_stringing_order(
            order=self._order(assigned_coach=self.iizuka), user=self.member
        )
        now = timezone.localtime(order.created_at)

        first = calculate_monthly_settlement(now.year, now.month, force=True)
        second = calculate_monthly_settlement(now.year, now.month, force=True)
        self.assertEqual(first["stringing_total"], 1200)
        self.assertEqual(second["stringing_total"], 1200)

        order.assigned_coach = self.shimizu
        order.full_clean()
        order.save(update_fields=["assigned_coach", "updated_at"])
        changed = calculate_monthly_settlement(now.year, now.month, force=True)
        amounts = {row["coach"].pk: row["stringing_amount"] for row in changed["coach_rows"]}
        self.assertEqual(amounts[self.iizuka.pk], 0)
        self.assertEqual(amounts[self.shimizu.pk], 1200)

        update_stringing_order_status(
            order_id=order.pk, new_status=StringingOrder.STATUS_CANCELED
        )
        canceled = calculate_monthly_settlement(now.year, now.month, force=True)
        self.assertEqual(canceled["stringing_total"], 0)

    def test_record_form_only_offers_members_and_supported_coaches(self):
        form = StringingOrderRecordForm()

        self.assertEqual(list(form.fields["user"].queryset), [self.member])
        self.assertEqual(
            set(form.fields["assigned_coach"].queryset),
            {self.iizuka, self.shimizu},
        )
        self.assertNotIn(self.inoue, form.fields["assigned_coach"].queryset)
        self.assertNotIn(self.contractor, form.fields["assigned_coach"].queryset)
        self.assertNotIn(self.inactive_iizuka, form.fields["assigned_coach"].queryset)

    def test_supported_coaches_can_record_completed_order_for_existing_member(self):
        self.iizuka.set_password("password")
        self.iizuka.save(update_fields=["password"])
        self.client.force_login(self.iizuka)
        user_count = get_user_model().objects.count()
        performed_date = timezone.localdate()

        form_page = self.client.get(reverse("club:stringing_order_record_create"))
        self.assertContains(form_page, "ガット張り実績を登録")

        for coach in (self.iizuka, self.shimizu):
            response = self.client.post(
                reverse("club:stringing_order_record_create"),
                {
                    "user": self.member.pk,
                    "assigned_coach": coach.pk,
                    "performed_date": performed_date.isoformat(),
                    "delivery_option": "0",
                    "tension_lbs": "50",
                    "racket_name": f"口頭依頼ラケット{coach.pk}",
                    "string_name": "",
                    "delivery_location": "",
                    "preferred_delivery_time": "",
                    "note": "口頭受付",
                },
            )
            self.assertEqual(response.status_code, 302)

        self.assertEqual(get_user_model().objects.count(), user_count)
        order = StringingOrder.objects.get(racket_name=f"口頭依頼ラケット{self.shimizu.pk}")
        self.assertEqual(order.user, self.member)
        self.assertEqual(order.assigned_coach, self.shimizu)
        self.assertEqual(order.status, StringingOrder.STATUS_COMPLETED)
        self.assertEqual(timezone.localtime(order.created_at).date(), performed_date)
        self.assertEqual(order.total_price(), 1200)

        self.client.force_login(self.member)
        self.member.is_profile_completed = True
        self.member.full_name = "ガット会員"
        self.member.email = "member@example.com"
        self.member.phone_number = "09000000000"
        self.member.save(update_fields=["is_profile_completed", "full_name", "email", "phone_number"])
        history = self.client.get(reverse("club:stringing_order_create"))
        self.assertContains(history, f"口頭依頼ラケット{self.shimizu.pk}")
        detail = self.client.get(reverse("club:stringing_order_detail", args=[order.pk]))
        self.assertEqual(detail.status_code, 200)

        self.client.force_login(self.contractor)
        forbidden = self.client.get(reverse("club:stringing_order_record_create"))
        self.assertEqual(forbidden.status_code, 403)

    def test_recorded_delivery_and_settlement_use_existing_source_once(self):
        performed_date = timezone.localdate()
        order = create_recorded_stringing_order(
            order=self._order(
                delivery_requested=True,
                delivery_location="テストコート",
                preferred_delivery_time="18時",
            ),
            user=self.member,
            assigned_coach=self.iizuka,
            performed_date=performed_date,
        )

        result = calculate_monthly_settlement(
            performed_date.year, performed_date.month, force=True
        )
        amounts = {row["coach"].pk: row["stringing_amount"] for row in result["coach_rows"]}
        self.assertEqual(order.total_price(), 1700)
        self.assertEqual(result["stringing_total"], 1700)
        self.assertEqual(amounts[self.iizuka.pk], 1700)

        update_stringing_order_status(
            order_id=order.pk,
            new_status=StringingOrder.STATUS_CANCELED,
        )
        canceled = calculate_monthly_settlement(
            performed_date.year, performed_date.month, force=True
        )
        canceled_amounts = {
            row["coach"].pk: row["stringing_amount"] for row in canceled["coach_rows"]
        }
        self.assertEqual(canceled["stringing_total"], 0)
        self.assertEqual(canceled_amounts[self.iizuka.pk], 0)

    def test_customer_order_form_still_creates_requested_order(self):
        self.member.is_profile_completed = True
        self.member.full_name = "ガット会員"
        self.member.email = "member@example.com"
        self.member.phone_number = "09000000000"
        self.member.save(update_fields=["is_profile_completed", "full_name", "email", "phone_number"])
        self.client.force_login(self.member)

        response = self.client.post(
            reverse("club:stringing_order_create"),
            {
                "delivery_option": "0",
                "tension_lbs": "50",
                "racket_name": "通常依頼ラケット",
                "string_name": "",
                "delivery_location": "",
                "preferred_delivery_time": "",
                "preferred_finish_date": (timezone.localdate() + timedelta(days=7)).isoformat(),
                "note": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        order = StringingOrder.objects.get(racket_name="通常依頼ラケット")
        self.assertEqual(order.user, self.member)
        self.assertEqual(order.status, StringingOrder.STATUS_REQUESTED)
        self.assertEqual(order.total_price(), 1200)
