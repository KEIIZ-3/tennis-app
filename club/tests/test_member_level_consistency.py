from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.admin import CustomUserChangeForm
from club.family_reservations import save_reservation_participant_snapshot
from club.models import CoachAvailability, Court, FamilyMember, Reservation


class MemberLevelConsistencyTests(TestCase):
    def setUp(self):
        self.User = get_user_model()
        self.member = self.User.objects.create_user(
            username="level-member",
            password="password12345",
            full_name="Level Member",
            email="level-member@example.com",
            phone_number="09000000001",
            role=self.User.ROLE_MEMBER,
            member_level=self.User.LEVEL_BEGINNER,
            is_profile_completed=True,
        )
        self.coach = self.User.objects.create_user(
            username="level-coach",
            password="password12345",
            role=self.User.ROLE_COACH,
            member_level=self.User.LEVEL_ADVANCED,
            is_profile_completed=True,
        )
        self.court = Court.objects.create(name="Level Court", is_active=True)
        lesson_date = timezone.localdate() + timedelta(days=10)
        self.start_at = timezone.make_aware(datetime.combine(lesson_date, time(19)))
        self.availability = CoachAvailability.objects.create(
            coach=self.coach,
            court=self.court,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=self.User.LEVEL_INTERMEDIATE,
            start_at=self.start_at,
            end_at=self.start_at + timedelta(hours=2),
            capacity=6,
            status=CoachAvailability.STATUS_OPEN,
        )
        self.client.force_login(self.member)

    def _admin_form_save(self, level):
        data = {
            "username": self.member.username,
            "full_name": self.member.full_name,
            "email": self.member.email,
            "phone_number": self.member.phone_number,
            "member_level": level,
            "ticket_balance": self.member.ticket_balance,
            "role": self.member.role,
            "contractor_hourly_wage": self.member.contractor_hourly_wage,
            "is_profile_completed": self.member.is_profile_completed,
            "is_active": self.member.is_active,
            "is_staff": self.member.is_staff,
            "is_superuser": self.member.is_superuser,
            "date_joined": self.member.date_joined,
        }
        form = CustomUserChangeForm(data=data, instance=self.member)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.member.refresh_from_db()

    def _confirm(self):
        with patch("club.views._require_schedule_survey", return_value=None):
            return self.client.get(
                reverse("club:lesson_reservation_confirm"),
                {
                    "availability_id": self.availability.pk,
                    "year": self.start_at.year,
                    "month": self.start_at.month,
                },
            )

    def test_admin_level_change_is_visible_without_relogin_and_controls_booking(self):
        beginner_response = self._confirm()
        self.assertContains(beginner_response, self.member.get_member_level_display())
        self.assertFalse(beginner_response.context["selected_lesson"]["participant_choices"][0]["can_book"])

        self._admin_form_save(self.User.LEVEL_INTERMEDIATE)
        self.assertEqual(self.member.member_level, self.User.LEVEL_INTERMEDIATE)
        upgraded_response = self._confirm()
        self.assertContains(upgraded_response, self.member.get_member_level_display())
        self.assertTrue(upgraded_response.context["selected_lesson"]["participant_choices"][0]["can_book"])

        self._admin_form_save(self.User.LEVEL_BEGINNER)
        downgraded_response = self._confirm()
        self.assertFalse(downgraded_response.context["selected_lesson"]["participant_choices"][0]["can_book"])

    def test_old_self_snapshot_does_not_override_current_member_level_display(self):
        reservation = Reservation.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court,
            availability=self.availability,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=self.start_at,
            end_at=self.start_at + timedelta(hours=2),
            status=Reservation.STATUS_ACTIVE,
        )
        save_reservation_participant_snapshot(
            reservation,
            {
                "type": "self",
                "name": self.member.display_name(),
                "level": self.User.LEVEL_BEGINNER,
                "level_label": self.User.level_label(self.User.LEVEL_BEGINNER),
                "relationship_label": "本人",
            },
        )
        self.User.objects.filter(pk=self.member.pk).update(member_level=self.User.LEVEL_INTERMEDIATE)

        response = self.client.get(reverse("club:reservation_detail", args=[reservation.pk]))

        self.assertContains(response, self.User.level_label(self.User.LEVEL_INTERMEDIATE))
        self.assertEqual(reservation.target_level, self.User.LEVEL_INTERMEDIATE)

    def test_family_level_remains_independent_from_parent_level(self):
        child = FamilyMember.objects.create(
            parent=self.member,
            full_name="Family Child",
            relationship="child",
            member_level=self.User.LEVEL_INTERMEDIATE,
        )
        reservation = Reservation.objects.create(
            user=self.member,
            coach=self.coach,
            court=self.court,
            availability=self.availability,
            lesson_type=Reservation.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            start_at=self.start_at,
            end_at=self.start_at + timedelta(hours=2),
            status=Reservation.STATUS_ACTIVE,
        )
        save_reservation_participant_snapshot(
            reservation,
            {
                "type": "family",
                "family_member_id": child.pk,
                "name": child.full_name,
                "level": child.member_level,
                "level_label": child.get_member_level_display(),
                "relationship_label": child.get_relationship_display(),
            },
        )
        self.User.objects.filter(pk=self.member.pk).update(member_level=self.User.LEVEL_ADVANCED)

        response = self.client.get(reverse("club:reservation_detail", args=[reservation.pk]))

        self.assertContains(response, child.get_member_level_display())
        self.assertNotContains(response, self.User.level_label(self.User.LEVEL_ADVANCED))
