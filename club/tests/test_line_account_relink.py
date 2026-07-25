from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from club.models import Court, FixedLesson, LineAccountLink


class LineAccountRelinkTests(TestCase):
    def setUp(self):
        self.User = get_user_model()
        self.court = Court.objects.create(
            name="LINE連携テストコート",
            is_active=True,
            court_type=Court.COURT_SONO,
        )
        self.coach = self._create_user(
            username="line_relink_coach",
            role=self.User.ROLE_COACH,
            full_name="飯塚 コーチ",
        )
        self.lesson_date = timezone.localdate() + timedelta(days=7)

    def _create_user(self, *, username, role, full_name):
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
        user.save()
        return user

    def _create_fixed_lesson(self, *, title):
        return FixedLesson.objects.create(
            title=title,
            coach=self.coach,
            court=self.court,
            lesson_type=FixedLesson.LESSON_GENERAL,
            target_level=self.User.LEVEL_BEGINNER,
            target_level_2="",
            start_date=self.lesson_date,
            weekday=self.lesson_date.weekday(),
            start_hour=19,
            capacity=6,
            coach_count=1,
            court_count=1,
            weeks_ahead=1,
            is_active=True,
        )

    def _create_line_provisional_user(self, *, username, line_user_id):
        user = self.User.objects.create_user(
            username=username,
            email="",
            password=None,
        )
        user.role = self.User.ROLE_MEMBER
        user.is_profile_completed = False
        user.save(update_fields=["role", "is_profile_completed"])
        LineAccountLink.objects.create(
            user=user,
            line_user_id=line_user_id,
            is_active=True,
        )
        return user

    def test_profile_completion_reuses_admin_created_member(self):
        admin_member = self._create_user(
            username="admin_created_member",
            role=self.User.ROLE_MEMBER,
            full_name="赤木 会員",
        )
        admin_member.email = "akagi@example.com"
        admin_member.phone_number = "090-1234-5678"
        admin_member.save(update_fields=["email", "phone_number"])
        fixed_lesson = self._create_fixed_lesson(
            title="管理画面登録レッスン",
        )
        fixed_lesson.members.add(admin_member)
        fixed_lesson.sync_future_reservations(created_by=self.coach)

        provisional = self._create_line_provisional_user(
            username="line_provisional",
            line_user_id="U-line-akagi",
        )
        self.client.force_login(provisional)

        response = self.client.post(
            reverse("club:profile_complete"),
            data={
                "full_name": "赤木 会員",
                "email": "akagi@example.com",
                "phone_number": "09012345678",
                "member_level": self.User.LEVEL_BEGINNER,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            int(self.client.session["_auth_user_id"]),
            admin_member.pk,
        )
        self.assertEqual(
            LineAccountLink.objects.get(line_user_id="U-line-akagi").user_id,
            admin_member.pk,
        )

        confirmation = self.client.get(reverse("club:reservation_list"))
        self.assertEqual(confirmation.status_code, 200)
        self.assertEqual(len(confirmation.context["future_reservation_rows"]), 1)
        self.assertEqual(
            confirmation.context["future_reservation_rows"][0][
                "reservation"
            ].fixed_lesson_id,
            fixed_lesson.pk,
        )

    def test_profile_completion_does_not_merge_phone_mismatch(self):
        existing_member = self._create_user(
            username="other_admin_member",
            role=self.User.ROLE_MEMBER,
            full_name="同姓 会員",
        )
        existing_member.email = "same@example.com"
        existing_member.phone_number = "09011112222"
        existing_member.save(update_fields=["email", "phone_number"])

        provisional = self._create_line_provisional_user(
            username="line_phone_mismatch",
            line_user_id="U-line-phone-mismatch",
        )
        self.client.force_login(provisional)

        response = self.client.post(
            reverse("club:profile_complete"),
            data={
                "full_name": "同姓 会員",
                "email": "same@example.com",
                "phone_number": "09099998888",
                "member_level": self.User.LEVEL_BEGINNER,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            int(self.client.session["_auth_user_id"]),
            provisional.pk,
        )
        self.assertEqual(
            LineAccountLink.objects.get(
                line_user_id="U-line-phone-mismatch"
            ).user_id,
            provisional.pk,
        )
