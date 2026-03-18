from datetime import timedelta

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    ROLE_CHOICES = (
        ("member", "member"),
        ("coach", "coach"),
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="member")

    def is_coach(self):
        return self.role == "coach"

    def __str__(self):
        return self.username


class Court(models.Model):
    name = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.name


class CoachAvailability(models.Model):
    coach = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="coach_availabilities",
        limit_choices_to={"role": "coach"},
    )
    court = models.ForeignKey(
        Court,
        on_delete=models.CASCADE,
        related_name="coach_availabilities",
    )
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    capacity = models.PositiveIntegerField(default=1)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start_at", "coach_id", "court_id"]

    def __str__(self):
        return f"{self.coach} / {self.court} / {self.start_at:%Y-%m-%d %H:%M}"

    def clean(self):
        if self.start_at >= self.end_at:
            raise ValidationError("開始日時は終了日時より前にしてください。")

        if (
            self.start_at.minute != 0
            or self.start_at.second != 0
            or self.start_at.microsecond != 0
            or self.end_at.minute != 0
            or self.end_at.second != 0
            or self.end_at.microsecond != 0
        ):
            raise ValidationError("コーチ空き時間は1時間単位で指定してください。")

        duration = self.end_at - self.start_at
        if duration.total_seconds() <= 0 or duration.total_seconds() % 3600 != 0:
            raise ValidationError("コーチ空き時間は1時間単位で指定してください。")

        if self.capacity < 1:
            raise ValidationError("定員は1以上にしてください。")

        overlap_qs = CoachAvailability.objects.filter(
            coach=self.coach,
            start_at__lt=self.end_at,
            end_at__gt=self.start_at,
        )
        if self.pk:
            overlap_qs = overlap_qs.exclude(pk=self.pk)
        if overlap_qs.exists():
            raise ValidationError("同じコーチで重複する空き時間があります。")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Reservation(models.Model):
    STATUS_ACTIVE = "active"
    STATUS_CANCELED = "canceled"

    STATUS_CHOICES = (
        (STATUS_ACTIVE, "active"),
        (STATUS_CANCELED, "canceled"),
    )

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="reservations",
    )
    coach = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="coach_reservations",
        limit_choices_to={"role": "coach"},
    )
    court = models.ForeignKey(
        Court,
        on_delete=models.CASCADE,
        related_name="reservations",
    )
    availability = models.ForeignKey(
        CoachAvailability,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reservations",
    )
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-start_at", "-id"]

    def __str__(self):
        return f"{self.user} / {self.coach} / {self.start_at:%Y-%m-%d %H:%M}"

    def clean(self):
        if self.start_at >= self.end_at:
            raise ValidationError("開始日時は終了日時より前にしてください。")

        if (
            self.start_at.minute != 0
            or self.start_at.second != 0
            or self.start_at.microsecond != 0
            or self.end_at.minute != 0
            or self.end_at.second != 0
            or self.end_at.microsecond != 0
        ):
            raise ValidationError("予約は1時間単位でのみ可能です。")

        if self.end_at - self.start_at != timedelta(hours=1):
            raise ValidationError("予約はちょうど1時間で指定してください。")

        if self.user_id and self.coach_id and self.user_id == self.coach_id:
            raise ValidationError("自分自身を予約することはできません。")

        if self.status == self.STATUS_CANCELED:
            return

        user_overlap_qs = Reservation.objects.filter(
            user=self.user,
            status=self.STATUS_ACTIVE,
            start_at__lt=self.end_at,
            end_at__gt=self.start_at,
        )
        if self.pk:
            user_overlap_qs = user_overlap_qs.exclude(pk=self.pk)
        if user_overlap_qs.exists():
            raise ValidationError("同じ時間帯にすでに別の予約があります。")

        availability_qs = CoachAvailability.objects.filter(
            coach=self.coach,
            court=self.court,
            start_at__lte=self.start_at,
            end_at__gte=self.end_at,
        ).order_by("start_at")

        availability = availability_qs.first()
        if not availability:
            raise ValidationError("該当するコーチ空き時間がありません。")

        self.availability = availability

        slot_reservations_qs = Reservation.objects.filter(
            coach=self.coach,
            court=self.court,
            start_at=self.start_at,
            end_at=self.end_at,
            status=self.STATUS_ACTIVE,
        )
        if self.pk:
            slot_reservations_qs = slot_reservations_qs.exclude(pk=self.pk)

        if slot_reservations_qs.count() >= availability.capacity:
            raise ValidationError("この時間枠は満員です。")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class LineAccountLink(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="line_link",
    )
    line_user_id = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)
    linked_at = models.DateTimeField(default=timezone.now)
    last_event_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["user_id"]

    def __str__(self):
        return f"{self.user.username} <-> {self.line_user_id}"
