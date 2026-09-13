from datetime import datetime, time, timedelta

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from club.admin import LessonWaitlistAdmin
from club.models import CoachAvailability, Court, LessonWaitlist, Reservation
from club.waitlist_service import promote_waitlist


class LessonWaitlistAdminSafetyTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.admin_user = user_model.objects.create_superuser(
            username="waitlist-admin",
            email="admin@example.com",
            password="password",
        )
        self.member = user_model.objects.create_user(
            username="waitlist-member",
            password="password",
            role=user_model.ROLE_MEMBER,
            member_level=user_model.LEVEL_BEGINNER,
            is_profile_completed=True,
        )
        self.coach = user_model.objects.create_user(
            username="waitlist-coach",
            password="password",
            role=user_model.ROLE_COACH,
        )
        self.court = Court.objects.create(name="Waitlist Admin Court")
        start_at = timezone.make_aware(
            datetime.combine(timezone.localdate() + timedelta(days=7), time(10))
        )
        self.availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=user_model.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=start_at + timedelta(hours=2),
            capacity=6,
        )
        self.waitlist = LessonWaitlist.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court,
            availability=self.availability,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=user_model.LEVEL_BEGINNER,
            start_at=start_at,
            end_at=start_at + timedelta(hours=2),
        )
        self.model_admin = LessonWaitlistAdmin(LessonWaitlist, admin.site)
        self.request = RequestFactory().get("/")
        self.request.user = self.admin_user

    def test_admin_detail_is_read_only(self):
        self.assertTrue(self.model_admin.has_view_permission(self.request, self.waitlist))
        self.assertFalse(self.model_admin.has_change_permission(self.request, self.waitlist))
        self.assertFalse(self.model_admin.has_add_permission(self.request))
        self.assertEqual(
            set(self.model_admin.get_readonly_fields(self.request, self.waitlist)),
            {
                field.name
                for field in LessonWaitlist._meta.fields
                if not field.auto_created
            },
        )

    def test_tampered_post_cannot_change_status(self):
        self.client.force_login(self.admin_user)
        url = reverse("admin:club_lessonwaitlist_change", args=[self.waitlist.pk])

        response = self.client.post(
            url,
            {"status": LessonWaitlist.STATUS_CONVERTED, "_save": "Save"},
        )

        self.assertEqual(response.status_code, 403)
        self.waitlist.refresh_from_db()
        self.assertEqual(self.waitlist.status, LessonWaitlist.STATUS_WAITING)
        self.assertFalse(Reservation.objects.filter(user=self.member).exists())

    def test_physical_and_bulk_delete_are_disabled(self):
        self.assertFalse(self.model_admin.has_delete_permission(self.request, self.waitlist))
        self.client.force_login(self.admin_user)
        delete_url = reverse("admin:club_lessonwaitlist_delete", args=[self.waitlist.pk])
        self.assertEqual(self.client.post(delete_url, {"post": "yes"}).status_code, 403)

        changelist_url = reverse("admin:club_lessonwaitlist_changelist")
        response = self.client.get(changelist_url)
        action_form = response.context["action_form"]
        action_values = (
            {value for value, _label in action_form.fields["action"].choices}
            if action_form is not None
            else set()
        )
        self.assertNotIn("delete_selected", action_values)
        self.assertTrue(LessonWaitlist.objects.filter(pk=self.waitlist.pk).exists())

    def test_admin_get_does_not_change_database(self):
        before = LessonWaitlist.objects.values().get(pk=self.waitlist.pk)
        self.client.force_login(self.admin_user)

        response = self.client.get(
            reverse("admin:club_lessonwaitlist_change", args=[self.waitlist.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(LessonWaitlist.objects.values().get(pk=self.waitlist.pk), before)

    def test_canonical_cancel_flow_sets_history_fields(self):
        self.assertTrue(self.waitlist.cancel(reason="admin safety test"))

        self.waitlist.refresh_from_db()
        self.assertEqual(self.waitlist.status, LessonWaitlist.STATUS_CANCELED)
        self.assertIsNotNone(self.waitlist.canceled_at)
        self.assertEqual(self.waitlist.note, "admin safety test")

    def test_canonical_promotion_creates_exactly_one_reservation(self):
        result = promote_waitlist(self.waitlist.pk, created_by=self.admin_user)

        self.waitlist.refresh_from_db()
        self.assertEqual(self.waitlist.status, LessonWaitlist.STATUS_CONVERTED)
        self.assertIsNotNone(self.waitlist.converted_at)
        self.assertEqual(
            Reservation.objects.filter(
                user=self.member,
                availability=self.availability,
            ).count(),
            1,
        )
        self.assertEqual(result.reservation.user_id, self.member.pk)

    def test_canonical_promotion_does_not_duplicate_existing_reservation(self):
        existing = Reservation.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court,
            availability=self.availability,
            lesson_type=self.availability.lesson_type,
            target_level=self.availability.target_level,
            start_at=self.availability.start_at,
            end_at=self.availability.end_at,
            status=Reservation.STATUS_ACTIVE,
        )

        result = promote_waitlist(self.waitlist.pk, created_by=self.admin_user)

        self.assertTrue(result.converted_existing)
        self.assertEqual(result.reservation.pk, existing.pk)
        self.assertEqual(
            Reservation.objects.filter(
                user=self.member,
                availability=self.availability,
            ).count(),
            1,
        )

    def test_canonical_promotion_does_not_exceed_capacity(self):
        for index in range(5):
            other_member = get_user_model().objects.create_user(
                username=f"capacity-member-{index}",
                password="password",
                role=get_user_model().ROLE_MEMBER,
                member_level=get_user_model().LEVEL_BEGINNER,
                is_profile_completed=True,
            )
            Reservation.objects.create(
                user=other_member,
                coach=self.coach,
                court=self.court,
                availability=self.availability,
                lesson_type=self.availability.lesson_type,
                target_level=self.availability.target_level,
                start_at=self.availability.start_at,
                end_at=self.availability.end_at,
                status=Reservation.STATUS_ACTIVE,
            )

        with self.assertRaises(ValidationError):
            promote_waitlist(self.waitlist.pk, created_by=self.admin_user)

        self.waitlist.refresh_from_db()
        self.assertEqual(self.waitlist.status, LessonWaitlist.STATUS_WAITING)
        self.assertEqual(
            Reservation.objects.filter(availability=self.availability).count(),
            5,
        )
