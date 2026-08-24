from datetime import datetime, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.models import (
    CoachAvailability,
    Court,
    FixedLesson,
    Reservation,
    TicketLedger,
    TicketConsumption,
    TicketPurchase,
    TicketPurchaseReservation,
    User,
)
from club.ticket_purchase_reservation_service import (
    _locked_purchase_reservations,
    approve_purchase_reservation,
    cancel_purchase_reservation,
    create_purchase_reservation,
    reverse_purchase_reservation,
    ticket_expiration_from,
)


class TicketPurchaseReservationServiceTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user(username="ticket-member", role=User.ROLE_MEMBER, ticket_balance=0)
        self.other = User.objects.create_user(username="other-member", role=User.ROLE_MEMBER)
        self.main_coach = User.objects.create_user(username="main-coach", role=User.ROLE_COACH)
        self.contractor = User.objects.create_user(username="contractor", role=User.ROLE_CONTRACTOR_COACH)

    def test_single_and_set_reservations_snapshot_prices_without_granting(self):
        single = create_purchase_reservation(user=self.member, product_code="single")
        set4 = create_purchase_reservation(user=self.member, product_code="set4")
        self.member.refresh_from_db()
        self.assertEqual((single.ticket_count, single.unit_price, single.total_amount), (1, 4000, 4000))
        self.assertEqual((set4.ticket_count, set4.unit_price, set4.total_amount), (4, 3500, 14000))
        self.assertEqual(self.member.ticket_balance, 0)
        self.assertFalse(TicketPurchase.objects.exists())
        self.assertFalse(TicketLedger.objects.exists())

    def test_approval_uses_formal_purchase_path_and_is_idempotent(self):
        pending = create_purchase_reservation(user=self.member, product_code="set4")
        approved, created = approve_purchase_reservation(reservation_id=pending.pk, coach=self.main_coach)
        repeated, repeated_created = approve_purchase_reservation(reservation_id=pending.pk, coach=self.main_coach)
        self.member.refresh_from_db()
        self.assertTrue(created)
        self.assertFalse(repeated_created)
        self.assertEqual(approved.pk, repeated.pk)
        self.assertEqual(self.member.ticket_balance, 4)
        self.assertEqual(TicketPurchase.objects.count(), 1)
        purchase = TicketPurchase.objects.get()
        self.assertEqual((purchase.total_tickets, purchase.unit_price), (4, 3500))
        self.assertEqual(purchase.created_by, self.main_coach)
        self.assertAlmostEqual(purchase.expires_at, ticket_expiration_from(purchase.purchased_at), delta=timedelta(seconds=1))
        pending.refresh_from_db()
        self.assertEqual(pending.status, TicketPurchaseReservation.STATUS_APPROVED)
        self.assertEqual(pending.ticket_purchase, purchase)
        self.assertEqual(pending.approved_by, self.main_coach)
        self.assertIsNotNone(pending.approved_at)

    def test_reservation_lock_query_does_not_join_nullable_relations(self):
        queryset = _locked_purchase_reservations()

        self.assertTrue(queryset.query.select_for_update)
        self.assertFalse(queryset.query.select_related)

    def test_contractor_cannot_approve(self):
        pending = create_purchase_reservation(user=self.member, product_code="single")
        with self.assertRaises(PermissionDenied):
            approve_purchase_reservation(reservation_id=pending.pk, coach=self.contractor)
        self.assertFalse(TicketPurchase.objects.exists())

    def test_unused_single_and_set4_can_be_reversed_with_audit_and_no_net_sales(self):
        for product_code, tickets, amount in (("single", 1, 4000), ("set4", 4, 14000)):
            before_balance = self.member.ticket_balance
            row = create_purchase_reservation(user=self.member, product_code=product_code)
            approved, _ = approve_purchase_reservation(reservation_id=row.pk, coach=self.main_coach)
            self.member.refresh_from_db()
            self.assertEqual(self.member.ticket_balance, before_balance + tickets)
            reversed_row, changed = reverse_purchase_reservation(
                reservation_id=approved.pk, coach=self.main_coach, reason="test"
            )
            self.assertTrue(changed)
            self.member.refresh_from_db()
            reversed_row.refresh_from_db()
            purchase = reversed_row.ticket_purchase
            purchase.refresh_from_db()
            self.assertEqual(self.member.ticket_balance, before_balance)
            self.assertEqual(reversed_row.status, TicketPurchaseReservation.STATUS_REVERSED)
            self.assertIsNotNone(reversed_row.reversed_at)
            self.assertEqual(reversed_row.reversed_by, self.main_coach)
            self.assertEqual(reversed_row.reversal_reason, "test")
            self.assertEqual((purchase.total_tickets, purchase.unit_price, purchase.remaining_tickets), (tickets, amount // tickets, 0))
            self.assertIsNotNone(purchase.reversed_at)
            self.assertEqual(
                sum(p.total_tickets * p.unit_price for p in TicketPurchase.objects.filter(reversed_at__isnull=True)),
                0,
            )
            self.assertTrue(TicketLedger.objects.filter(reason=TicketLedger.REASON_PURCHASE_REVERSAL, change_amount=-tickets).exists())

    def test_reversal_is_idempotent_and_reapproval_is_forbidden(self):
        row = create_purchase_reservation(user=self.member, product_code="set4")
        approve_purchase_reservation(reservation_id=row.pk, coach=self.main_coach)
        _row, first = reverse_purchase_reservation(reservation_id=row.pk, coach=self.main_coach, reason="mistake")
        _row, second = reverse_purchase_reservation(reservation_id=row.pk, coach=self.main_coach, reason="mistake")
        self.assertTrue(first)
        self.assertFalse(second)
        self.member.refresh_from_db()
        self.assertEqual(self.member.ticket_balance, 0)
        self.assertEqual(TicketLedger.objects.filter(reason=TicketLedger.REASON_PURCHASE_REVERSAL).count(), 1)
        with self.assertRaises(ValidationError):
            approve_purchase_reservation(reservation_id=row.pk, coach=self.main_coach)

    def test_consumed_purchase_cannot_be_reversed_and_nothing_changes(self):
        row = create_purchase_reservation(user=self.member, product_code="set4")
        approved, _ = approve_purchase_reservation(reservation_id=row.pk, coach=self.main_coach)
        purchase = approved.ticket_purchase
        purchase.remaining_tickets = 3
        purchase.save(update_fields=["remaining_tickets"])
        consumption = TicketConsumption.objects.create(user=self.member, purchase=purchase, tickets_used=1, unit_price_snapshot=3500)
        self.member.refresh_from_db()
        before = (self.member.ticket_balance, purchase.remaining_tickets, TicketConsumption.objects.count(), TicketLedger.objects.count())
        with self.assertRaisesMessage(ValidationError, "既に使用されているため"):
            reverse_purchase_reservation(reservation_id=row.pk, coach=self.main_coach, reason="mistake")
        self.member.refresh_from_db()
        purchase.refresh_from_db()
        consumption.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual((self.member.ticket_balance, purchase.remaining_tickets, TicketConsumption.objects.count(), TicketLedger.objects.count()), before)
        self.assertEqual(row.status, TicketPurchaseReservation.STATUS_APPROVED)

    def test_contractor_cannot_reverse(self):
        row = create_purchase_reservation(user=self.member, product_code="single")
        approve_purchase_reservation(reservation_id=row.pk, coach=self.main_coach)
        with self.assertRaises(PermissionDenied):
            reverse_purchase_reservation(reservation_id=row.pk, coach=self.contractor, reason="test")

    def test_canceled_reservation_cannot_be_approved(self):
        pending = create_purchase_reservation(user=self.member, product_code="single")
        cancel_purchase_reservation(reservation_id=pending.pk, user=self.member)
        with self.assertRaises(ValidationError):
            approve_purchase_reservation(reservation_id=pending.pk, coach=self.main_coach)
        self.member.refresh_from_db()
        self.assertEqual(self.member.ticket_balance, 0)

    def test_user_cannot_cancel_another_users_reservation(self):
        pending = create_purchase_reservation(user=self.member, product_code="single")
        with self.assertRaises(TicketPurchaseReservation.DoesNotExist):
            cancel_purchase_reservation(reservation_id=pending.pk, user=self.other)

    def test_customer_create_endpoint_does_not_create_formal_purchase(self):
        self.client.force_login(self.member)
        response = self.client.post(reverse("club:ticket_purchase_reservation_create"), {"product": "single"})
        self.assertRedirects(response, reverse("club:tickets"), fetch_redirect_response=False)
        self.assertEqual(TicketPurchaseReservation.objects.filter(user=self.member).count(), 1)
        self.assertFalse(TicketPurchase.objects.exists())
        tickets = self.client.get(reverse("club:tickets"))
        self.assertContains(tickets, "承認待ち")
        self.assertContains(tickets, "1枚・4000円")

    def test_other_customer_cannot_cancel_via_direct_post(self):
        pending = create_purchase_reservation(user=self.member, product_code="single")
        self.client.force_login(self.other)
        response = self.client.post(reverse("club:ticket_purchase_reservation_cancel", args=[pending.pk]))
        self.assertEqual(response.status_code, 403)
        pending.refresh_from_db()
        self.assertEqual(pending.status, TicketPurchaseReservation.STATUS_PENDING)

    def test_contractor_cannot_open_or_post_approval_urls(self):
        pending = create_purchase_reservation(user=self.member, product_code="single")
        self.client.force_login(self.contractor)
        self.assertEqual(self.client.get(reverse("club:ticket_purchase_confirm")).status_code, 403)
        self.assertEqual(self.client.post(reverse("club:ticket_purchase_approve", args=[pending.pk])).status_code, 403)
        self.assertFalse(TicketPurchase.objects.exists())


class TicketPurchaseReservationCoachFlowTests(TestCase):
    def setUp(self):
        self.main_coach = User.objects.create_user(
            username="purchase-main-coach", role=User.ROLE_COACH
        )
        self.contractor = User.objects.create_user(
            username="purchase-contractor", role=User.ROLE_CONTRACTOR_COACH
        )
        self.member = User.objects.create_user(
            username="purchase-member", role=User.ROLE_MEMBER, full_name="飯塚セカンド"
        )
        self.other_member = User.objects.create_user(
            username="purchase-other-member", role=User.ROLE_MEMBER, full_name="購入 二人目"
        )
        self.non_participant = User.objects.create_user(
            username="purchase-non-participant", role=User.ROLE_MEMBER
        )
        self.court = Court.objects.create(name="購入予約テストコート")
        self.start_at = timezone.make_aware(
            datetime.combine(timezone.localdate(), datetime.min.time()).replace(hour=10)
        )
        self.end_at = self.start_at + timedelta(hours=2)
        self.availability = CoachAvailability.objects.create(
            coach=self.main_coach,
            coach_2=self.contractor,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            start_at=self.start_at,
            end_at=self.end_at,
            capacity=6,
        )
        self.reservation = self._reservation(self.member)

    def _reservation(self, user, *, status=Reservation.STATUS_ACTIVE, **extra):
        values = {
            "user": user,
            "coach": self.main_coach,
            "court": self.court,
            "availability": self.availability,
            "lesson_type": Reservation.LESSON_GENERAL,
            "target_level": User.LEVEL_BEGINNER,
            "start_at": self.start_at,
            "end_at": self.end_at,
            "status": status,
        }
        values.update(extra)
        return Reservation.objects.create(**values)

    def _member_list(self, user=None, **query):
        self.client.force_login(user or self.main_coach)
        params = {"availability_id": self.availability.pk}
        params.update(query)
        return self.client.get(reverse("club:lesson_calendar_member_list"), params)

    def test_member_list_shows_each_pending_product_and_customer(self):
        create_purchase_reservation(user=self.member, product_code="single")
        create_purchase_reservation(user=self.member, product_code="set4")
        self._reservation(self.other_member)
        create_purchase_reservation(user=self.other_member, product_code="single")

        response = self._member_list()

        self.assertContains(response, "チケット購入予約")
        # Main coaches also see both participants in each burden-payer selector.
        self.assertContains(response, "飯塚セカンド", count=6)
        self.assertContains(response, "購入 二人目", count=5)
        self.assertContains(response, "1枚")
        self.assertContains(response, "4000円")
        self.assertContains(response, "4枚セット")
        self.assertContains(response, "14000円")
        self.assertContains(response, "現金受領・承認", count=3)
        self.assertEqual(response.context["purchase_reservation_count"], 3)

    def test_only_pending_purchases_of_active_reservation_users_are_shown(self):
        visible = create_purchase_reservation(user=self.member, product_code="single")
        canceled_participant = User.objects.create_user(
            username="canceled-participant", role=User.ROLE_MEMBER
        )
        self._reservation(canceled_participant, status=Reservation.STATUS_CANCELED)
        canceled_participant_purchase = create_purchase_reservation(
            user=canceled_participant, product_code="single"
        )
        non_participant_purchase = create_purchase_reservation(
            user=self.non_participant, product_code="single"
        )
        canceled_purchase = create_purchase_reservation(
            user=self.member, product_code="single"
        )
        cancel_purchase_reservation(
            reservation_id=canceled_purchase.pk, user=self.member
        )
        approved_purchase = create_purchase_reservation(
            user=self.member, product_code="single"
        )
        approve_purchase_reservation(
            reservation_id=approved_purchase.pk, coach=self.main_coach
        )

        response = self._member_list()

        self.assertEqual(
            [row.pk for row in response.context["purchase_reservations"]],
            [visible.pk],
        )
        displayed_ids = response.content.decode()
        self.assertNotIn(f"予約番号 #{canceled_participant_purchase.pk}", displayed_ids)
        self.assertNotIn(f"予約番号 #{non_participant_purchase.pk}", displayed_ids)
        self.assertNotIn(f"予約番号 #{canceled_purchase.pk}", displayed_ids)
        self.assertNotIn(f"予約番号 #{approved_purchase.pk}", displayed_ids)

    def test_fixed_membership_alone_is_excluded_but_fixed_occurrence_is_supported(self):
        fixed = FixedLesson.objects.create(
            title="購入予約固定レッスン",
            coach=self.main_coach,
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_date=timezone.localdate(),
            weekday=timezone.localdate().weekday(),
            start_hour=14,
            capacity=6,
            is_active=True,
        )
        fixed.members.through.objects.create(
            fixedlesson_id=fixed.pk, user_id=self.non_participant.pk
        )
        fixed_start, fixed_end = fixed._build_datetimes_for_date(timezone.localdate())
        fixed_participant = User.objects.create_user(
            username="fixed-purchase-participant", role=User.ROLE_MEMBER
        )
        Reservation.objects.bulk_create([Reservation(
            user=fixed_participant,
            coach=self.main_coach,
            court=self.court,
            fixed_lesson=fixed,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_at=fixed_start,
            end_at=fixed_end,
            status=Reservation.STATUS_ACTIVE,
        )])
        included = create_purchase_reservation(
            user=fixed_participant, product_code="set4"
        )
        excluded = create_purchase_reservation(
            user=self.non_participant, product_code="single"
        )

        response = self._member_list(
            fixed_lesson_id=fixed.pk,
            lesson_date=timezone.localdate().isoformat(),
            availability_id="",
        )

        ids = [row.pk for row in response.context["purchase_reservations"]]
        self.assertEqual(ids, [included.pk])
        self.assertNotIn(excluded.pk, ids)

    def test_calendar_card_shows_pending_count_and_links_to_member_list(self):
        create_purchase_reservation(user=self.member, product_code="single")
        self.client.force_login(self.main_coach)

        response = self.client.get(
            reverse("club:lesson_calendar"),
            {"year": self.start_at.year, "month": self.start_at.month},
        )

        row = next(
            item
            for item in response.context["schedule_rows"]
            if item["availability_id"] == str(self.availability.pk)
        )
        self.assertEqual(row["pending_ticket_purchase_count"], 1)
        self.assertContains(response, "🎫 購入予約 1件")
        self.assertContains(response, row["member_list_url"].replace("&", "&amp;"))

    def test_contractor_sees_no_purchase_section_and_direct_post_is_forbidden(self):
        pending = create_purchase_reservation(user=self.member, product_code="single")

        response = self._member_list(user=self.contractor)

        self.assertNotContains(response, "チケット購入予約")
        approve_url = reverse("club:ticket_purchase_approve", args=[pending.pk])
        post_response = self.client.post(
            approve_url, {"availability_id": self.availability.pk}
        )
        self.assertEqual(post_response.status_code, 403)

    def test_approval_redirects_to_member_list_grants_once_and_removes_pending(self):
        pending = create_purchase_reservation(user=self.member, product_code="set4")
        self.client.force_login(self.main_coach)
        approve_url = reverse("club:ticket_purchase_approve", args=[pending.pk])
        query = f"?availability_id={self.availability.pk}"

        first = self.client.post(approve_url + query, follow=True)
        second = self.client.post(approve_url + query)

        expected = f"{reverse('club:lesson_calendar_member_list')}{query}"
        self.assertEqual(first.redirect_chain, [(expected, 302)])
        self.assertContains(first, "現金受領を確認し、4枚のチケットを付与しました。")
        self.assertContains(first, "チケット購入予約はありません。")
        self.assertRedirects(second, expected, fetch_redirect_response=False)
        self.member.refresh_from_db()
        self.assertEqual(self.member.ticket_balance, 4)
        self.assertEqual(TicketPurchase.objects.count(), 1)
        member_list = self._member_list()
        self.assertEqual(member_list.context["purchase_reservation_count"], 0)
        self.assertContains(member_list, "チケット購入予約はありません。")

    def test_lesson_without_purchase_does_not_show_calendar_badge(self):
        response = self._member_list()
        self.assertEqual(response.context["purchase_reservation_count"], 0)
        self.assertContains(response, "チケット購入予約はありません。")

        calendar = self.client.get(
            reverse("club:lesson_calendar"),
            {"year": self.start_at.year, "month": self.start_at.month},
        )
        row = next(
            item
            for item in calendar.context["schedule_rows"]
            if item["availability_id"] == str(self.availability.pk)
        )
        self.assertEqual(row["pending_ticket_purchase_count"], 0)
        self.assertNotContains(calendar, "🎫 購入予約")
