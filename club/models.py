from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models

class User(AbstractUser):
    ROLE_CHOICES = (
        ("customer", "Customer"),
        ("coach", "Coach"),
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default="customer")


class Court(models.Model):
    name = models.CharField(max_length=50, unique=True)  # 例: Aコート, Bコート
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Reservation(models.Model):
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reservations",
    )
    court = models.ForeignKey(Court, on_delete=models.PROTECT, related_name="reservations")
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()

    STATUS_CHOICES = (
        ("booked", "Booked"),
        ("cancelled", "Cancelled"),
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="booked")

    created_at = models.DateTimeField(auto_now_add=True)

class CoachAvailability(models.Model):
    coach = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="coach_availabilities",
    )
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()

    STATUS_CHOICES = (
        ("available", "Available"),
        ("unavailable", "Unavailable"),
    )
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="available")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-start_time"]

    def clean(self):
        if self.end_time <= self.start_time:
            raise ValidationError("終了時刻は開始時刻より後にしてください。")

        # 同じコーチで、available同士が重なるのを禁止（unavailableは後で使えるように残す）
        qs = CoachAvailability.objects.filter(
            coach=self.coach,
            date=self.date,
            status="available",
        ).exclude(pk=self.pk)

        overlap = qs.filter(start_time__lt=self.end_time, end_time__gt=self.start_time).exists()
        if overlap and self.status == "available":
            raise ValidationError("同じ時間帯に既に空き枠があります。")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.date} {self.start_time}-{self.end_time} ({self.coach})"

    class Meta:
        ordering = ["-date", "-start_time"]
        # ★ダブルブッキング防止（同じコート・同じ日・同じ開始時刻）
        constraints = [
            models.UniqueConstraint(
                fields=["court", "date", "start_time"],
                name="uniq_reservation_court_date_start",
            ),
        ]

    def clean(self):
        # 時刻の整合
        if self.end_time <= self.start_time:
            raise ValidationError("終了時刻は開始時刻より後にしてください。")

        # ★時間帯が重なる予約を防止（より厳密）
        qs = Reservation.objects.filter(
            court=self.court,
            date=self.date,
            status="booked",
        ).exclude(pk=self.pk)

        # 重なり判定: (start < other_end) and (end > other_start)
        overlap = qs.filter(start_time__lt=self.end_time, end_time__gt=self.start_time).exists()
        if overlap:
            raise ValidationError("同じコート・同じ時間帯に既に予約があります。")

    def save(self, *args, **kwargs):
        self.full_clean()  # clean() を必ず通す
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.date} {self.start_time}-{self.end_time} {self.court} ({self.customer})"

