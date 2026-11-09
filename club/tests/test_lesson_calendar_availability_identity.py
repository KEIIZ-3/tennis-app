from datetime import datetime

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.lesson_calendar_service import build_lesson_calendar_display_data, lesson_calendar_slot_key
from club.models import CoachAvailability, Court, LessonWaitlist, Reservation, TicketLedger, User


class LessonCalendarAvailabilityIdentityTests(TestCase):
    def setUp(self):
        self.old_coach = User.objects.create_user(
            username="availability-old-coach",
            role=User.ROLE_COACH,
            full_name="飯塚研太朗",
        )
        self.new_coach = User.objects.create_user(
            username="availability-new-coach",
            role=User.ROLE_COACH,
            full_name="井上春佳",
        )
        self.members = [
            self._member("availability-member-1", "二谷典子"),
            self._member("availability-member-2", "小倉真紀"),
        ]
        self.pending_member = self._member("availability-pending-member", "承認待ち会員")
        self.waiting_member = self._member("availability-waiting-member", "待機会員")
        self.other_member = self._member("availability-other-member", "別開催回会員")
        self.court = Court.objects.create(name="西猪名公園")
        self.start_at = timezone.make_aware(datetime(2026, 11, 20, 19, 0))
        self.end_at = timezone.make_aware(datetime(2026, 11, 20, 21, 0))
        self.availability = CoachAvailability.objects.create(
            coach=self.old_coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_at=self.start_at,
            end_at=self.end_at,
            status=CoachAvailability.STATUS_OPEN,
            capacity=5,
            court_count=1,
        )
        for member in self.members:
            self._reservation(member, self.availability, Reservation.STATUS_ACTIVE)
        self._reservation(self.pending_member, self.availability, Reservation.STATUS_PENDING)
        LessonWaitlist.objects.create(
            user=self.waiting_member,
            coach=self.old_coach,
            court=self.court,
            availability=self.availability,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_at=self.start_at,
            end_at=self.end_at,
        )

    def _member(self, username, full_name):
        return User.objects.create_user(
            username=username,
            role=User.ROLE_MEMBER,
            full_name=full_name,
            email=f"{username}@example.com",
            phone_number="09000000000",
            is_profile_completed=True,
            member_level=User.LEVEL_BEGINNER,
        )

    def _reservation(self, user, availability, status):
        return Reservation.objects.create(
            user=user,
            coach=self.old_coach,
            court=self.court,
            availability=availability,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_at=self.start_at,
            end_at=self.end_at,
            status=status,
        )

    def _calendar_item(self, user=None, availability=None):
        if user:
            self.client.force_login(user)
        response = self.client.get(
            reverse("club:lesson_calendar"),
            {"year": 2026, "month": 11},
        )
        self.assertEqual(response.status_code, 200)
        target = availability or self.availability
        return next(
            row
            for row in response.context["schedule_rows"]
            if row["availability_id"] == str(target.pk)
        )

    def _change_occurrence_coach(self):
        self.availability.coach = self.new_coach
        self.availability.save(update_fields=["coach"])

    def test_coach_change_keeps_relation_based_counts_member_list_and_user_states(self):
        before_ids = list(
            Reservation.objects.filter(availability=self.availability).order_by("pk").values_list("pk", flat=True)
        )
        ticket_history_count = TicketLedger.objects.count()

        self._change_occurrence_coach()

        item = self._calendar_item(self.members[0])
        self.assertEqual(item["member_count"], 2)
        self.assertEqual(item["pending_count"], 1)
        self.assertEqual(item["capacity_consuming_count"], 3)
        self.assertEqual(item["capacity"], 5)
        self.assertEqual(item["remaining_count"], 2)
        self.assertTrue(item["is_reserved_by_user"])
        self.assertEqual(item["disabled_reason"], "予約済みです。")

        self.client.force_login(self.old_coach)
        member_response = self.client.get(
            reverse("club:lesson_calendar_member_list"),
            {"availability_id": self.availability.pk, "year": 2026, "month": 11},
        )
        self.assertEqual(member_response.status_code, 200)
        self.assertEqual(len(member_response.context["active_rows"]), item["member_count"])

        self.assertEqual(
            list(
                Reservation.objects.filter(availability=self.availability)
                .order_by("pk")
                .values_list("pk", flat=True)
            ),
            before_ids,
        )
        self.assertEqual(
            set(
                Reservation.objects.filter(availability=self.availability)
                .values_list("coach_id", flat=True)
            ),
            {self.old_coach.pk},
        )
        self.assertEqual(TicketLedger.objects.count(), ticket_history_count)

    def test_waitlist_survives_coach_change_and_can_be_canceled(self):
        self._change_occurrence_coach()

        item = self._calendar_item(self.waiting_member)
        self.assertEqual(item["waitlist_count"], 1)
        self.assertTrue(item["is_waitlisted_by_user"])
        self.assertEqual(item["disabled_reason"], "キャンセル待ち中です。")
        self.assertTrue(item["can_cancel_waitlist"])

        response = self.client.post(
            reverse("club:lesson_calendar"),
            {
                "action": "cancel_waitlist",
                "availability_id": self.availability.pk,
                "year": 2026,
                "month": 11,
            },
        )
        self.assertEqual(response.status_code, 302)
        waitlist = LessonWaitlist.objects.get(availability=self.availability)
        self.assertEqual(waitlist.status, LessonWaitlist.STATUS_CANCELED)

    def test_same_physical_slot_different_availability_is_not_merged(self):
        other = CoachAvailability.objects.create(
            coach=self.new_coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_at=self.start_at,
            end_at=self.end_at,
            status=CoachAvailability.STATUS_OPEN,
            capacity=1,
            capacity_overridden=True,
        )
        Reservation.objects.bulk_create([Reservation(
            user=self.other_member,
            coach=self.old_coach,
            court=self.court,
            availability=other,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_at=self.start_at,
            end_at=self.end_at,
            status=Reservation.STATUS_ACTIVE,
        )])
        CoachAvailability.objects.filter(pk=self.availability.pk).update(coach=self.new_coach)
        self.availability.refresh_from_db()

        first_item = self._calendar_item(availability=self.availability)
        other_item = self._calendar_item(availability=other)

        self.assertEqual(first_item["member_count"], 2)
        self.assertEqual(first_item["pending_count"], 1)
        self.assertEqual(other_item["member_count"], 1)
        self.assertEqual(other_item["remaining_count"], 0)
        self.assertEqual(other_item["customer_status_label"], "満員")

    def test_existing_participant_cannot_book_again_after_coach_change(self):
        self._change_occurrence_coach()
        self.client.force_login(self.members[0])

        response = self.client.post(
            reverse("club:lesson_calendar"),
            {
                "action": "reserve",
                "availability_id": self.availability.pk,
                "participant_key": "self",
                "year": 2026,
                "month": 11,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Reservation.objects.filter(
                availability=self.availability,
                user=self.members[0],
                status__in=(Reservation.STATUS_ACTIVE, Reservation.STATUS_PENDING),
            ).count(),
            1,
        )

    def test_relationless_legacy_rows_remain_in_physical_fallback_maps(self):
        legacy_member = self._member("availability-legacy-member", "旧データ会員")
        legacy = Reservation(
            user=legacy_member,
            coach=self.old_coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=User.LEVEL_BEGINNER,
            start_at=self.start_at,
            end_at=self.end_at,
            status=Reservation.STATUS_ACTIVE,
        )
        Reservation.objects.bulk_create([legacy])
        month_start = self.start_at.date().replace(day=1)
        display_data = build_lesson_calendar_display_data(
            user=legacy_member,
            target_year=2026,
            target_month=11,
            month_start=month_start,
            next_month=month_start.replace(month=12),
        )
        slot_key = lesson_calendar_slot_key(
            lesson_type=legacy.lesson_type,
            coach_id=legacy.coach_id,
            court_id=legacy.court_id,
            start_at=legacy.start_at,
            end_at=legacy.end_at,
        )

        self.assertEqual(sum(display_data["active_slot_counts"].values()), 1)
        self.assertEqual(
            set(display_data["user_slot_status_map"].values()),
            {Reservation.STATUS_ACTIVE},
        )
        self.assertEqual(slot_key[0:3], (legacy.lesson_type, legacy.coach_id, legacy.court_id))
        self.assertNotIn(legacy, display_data["reservations_by_availability"].get(self.availability.pk, []))
