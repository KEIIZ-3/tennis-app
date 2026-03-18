from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone


class User(AbstractUser):
    ROLE_CHOICES = (
        ("customer", "Customer"),
        ("coach", "Coach"),
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default="customer")
    color = models.CharField(max_length=7, default="#2ecc71")

    def __str__(self):
        return self.username


class Court(models.Model):
    name = models.CharField(max_length=50, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class BusinessHours(models.Model):
    WEEKDAYS = [
        (0, "Mon"),
        (1, "Tue"),
        (2, "Wed"),
        (3, "Thu"),
        (4, "Fri"),
        (5, "Sat"),
        (6, "Sun"),
    ]

    weekday = models.IntegerField(choices=WEEKDAYS, unique=True)
    open_time = models.TimeField()
    close_time = models.TimeField()
    is_closed = models.BooleanField(default=False)

    class Meta:
        ordering = ["weekday"]

    def __str__(self):
        suffix = " (closed)" if self.is_closed else ""
        return f"{self.get_weekday_display()} {self.open_time}-{self.close_time}{suffix}"


class FacilityClosure(models.Model):
    date = models.DateField(unique=True)
    reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"{self.date} {self.reason}".strip()


class TicketWallet(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ticket_wallet",
    )
    balance = models.IntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["user__username"]

    def __str__(self):
        return f"{self.user} balance={self.balance}"


class TicketTransaction(models.Model):
    REASONS = [
        ("purchase", "Purchase"),
        ("admin_adjust", "Admin adjust"),
        ("consume", "Consume"),
        ("refund", "Refund"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ticket_transactions",
    )
    delta = models.IntegerField()
    reason = models.CharField(max_length=30, choices=REASONS)
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    reservation = models.ForeignKey(
        "Reservation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ticket_transactions",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} {self.delta} ({self.reason})"


def _validate_business_rules(date, start_time, end_time):
    if FacilityClosure.objects.filter(date=date).exists():
        raise ValidationError("休館日のため予約できません。")

    weekday = date.weekday()
    bh = BusinessHours.objects.filter(weekday=weekday).first()
    if not bh:
        raise ValidationError("営業時間が未設定です（管理者が設定してください）。")
    if bh.is_closed:
        raise ValidationError("定休日のため予約できません。")

    if start_time < bh.open_time or end_time > bh.close_time:
        raise ValidationError("営業時間外のため予約できません。")


class Reservation(models.Model):
    STATUS_CHOICES = (
        ("booked", "Booked"),
        ("cancelled", "Cancelled"),
    )

    KIND_CHOICES = (
        ("private_lesson", "Private lesson"),
        ("group_lesson", "Group lesson"),
        ("court_rental", "Court rental"),
    )

    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reservations",
    )
    coach = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="coach_reservations",
        null=True,
        blank=True,
    )
    court = models.ForeignKey(
        Court,
        on_delete=models.PROTECT,
        related_name="reservations",
    )
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="booked")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default="private_lesson")
    tickets_used = models.PositiveIntegerField(default=1)
    note = models.CharField(max_length=400, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "-start_time", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["court", "date", "start_time"],
                name="uniq_reservation_court_date_start",
            ),
        ]

    def __str__(self):
        coach_part = f" coach={self.coach}" if self.coach_id else ""
        return f"{self.date} {self.start_time}-{self.end_time} {self.court} ({self.customer}){coach_part}"

    @property
    def start_at(self):
        return timezone.make_aware(
            datetime.combine(self.date, self.start_time),
            timezone.get_current_timezone(),
        )

    @property
    def end_at(self):
        return timezone.make_aware(
            datetime.combine(self.date, self.end_time),
            timezone.get_current_timezone(),
        )

    @property
    def is_future(self) -> bool:
        return self.end_at > timezone.now()

    @property
    def cancel_deadline_at(self):
        hours = int(getattr(settings, "RESERVATION_CANCEL_DEADLINE_HOURS", 2))
        return self.start_at - timedelta(hours=hours)

    @property
    def can_cancel_now(self) -> bool:
        if self.status != "booked":
            return False
        return timezone.now() < self.cancel_deadline_at

    def clean(self):
        errors = []

        if self.customer_id and getattr(self.customer, "role", "customer") != "customer":
            errors.append("予約者は customer ロールである必要があります。")

        if self.coach_id and getattr(self.coach, "role", "") != "coach":
            errors.append("coach には coach ロールのユーザーを指定してください。")

        if self.end_time <= self.start_time:
            errors.append("終了時刻は開始時刻より後にしてください。")

        if self.status != "cancelled":
            _validate_business_rules(self.date, self.start_time, self.end_time)

            if self.date < timezone.localdate():
                errors.append("過去日の予約は作成できません。")

        qs_court = Reservation.objects.filter(
            court=self.court,
            date=self.date,
            status="booked",
        ).exclude(pk=self.pk)

        overlap_court = qs_court.filter(
            start_time__lt=self.end_time,
            end_time__gt=self.start_time,
        ).exists()
        if self.status == "booked" and overlap_court:
            errors.append("同じコート・同じ時間帯に既に予約があります。")

        if self.coach_id:
            qs_coach = Reservation.objects.filter(
                coach_id=self.coach_id,
                date=self.date,
                status="booked",
            ).exclude(pk=self.pk)

            overlap_coach = qs_coach.filter(
                start_time__lt=self.end_time,
                end_time__gt=self.start_time,
            )

            if self.status == "booked" and self.kind == "private_lesson" and overlap_coach.exists():
                errors.append("同じコーチが同じ時間帯に既に予約されています。")

            av = (
                CoachAvailability.objects.filter(
                    coach_id=self.coach_id,
                    date=self.date,
                    status="available",
                    start_time__lte=self.start_time,
                    end_time__gte=self.end_time,
                )
                .order_by("start_time")
                .first()
            )

            if self.status == "booked" and not av:
                errors.append("この時間帯のコーチ空き枠がありません。")

            if self.status == "booked" and av and self.kind == "group_lesson":
                booked_count = (
                    Reservation.objects.filter(
                        coach_id=self.coach_id,
                        date=self.date,
                        status="booked",
                        kind="group_lesson",
                        start_time__lt=self.end_time,
                        end_time__gt=self.start_time,
                    )
                    .exclude(pk=self.pk)
                    .count()
                )
                if booked_count + 1 > av.capacity:
                    errors.append("このコーチ枠は満員です。")

        if self.kind in ("private_lesson", "group_lesson"):
            if self.tickets_used < 1:
                errors.append("レッスン予約は tickets_used を1以上にしてください。")
            if not self.coach_id:
                errors.append("レッスン予約にはコーチ指定が必要です。")
        else:
            if self.tickets_used != 0:
                self.tickets_used = 0

        if errors:
            raise ValidationError(errors)

    @transaction.atomic
    def _consume_tickets(self):
        if self.status != "booked":
            return
        if self.kind not in ("private_lesson", "group_lesson"):
            return
        if self.tickets_used <= 0:
            return

        wallet, _ = TicketWallet.objects.select_for_update().get_or_create(user=self.customer)
        if wallet.balance < self.tickets_used:
            raise ValidationError(f"チケット残数が足りません（残:{wallet.balance} / 必要:{self.tickets_used}）。")

        wallet.balance -= self.tickets_used
        wallet.save(update_fields=["balance", "updated_at"])

        TicketTransaction.objects.create(
            user=self.customer,
            delta=-int(self.tickets_used),
            reason="consume",
            note="Consumed by reservation",
            reservation=self,
        )

    @transaction.atomic
    def _refund_tickets(self):
        if self.kind not in ("private_lesson", "group_lesson"):
            return
        if self.tickets_used <= 0:
            return

        wallet, _ = TicketWallet.objects.select_for_update().get_or_create(user=self.customer)
        wallet.balance += self.tickets_used
        wallet.save(update_fields=["balance", "updated_at"])

        TicketTransaction.objects.create(
            user=self.customer,
            delta=+int(self.tickets_used),
            reason="refund",
            note="Refund by cancellation",
            reservation=self,
        )

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        old_status = None

        if not is_new:
            old_status = (
                Reservation.objects.filter(pk=self.pk)
                .values_list("status", flat=True)
                .first()
            )

        self.full_clean()
        super().save(*args, **kwargs)

        if is_new and self.status == "booked":
            self._consume_tickets()
        elif old_status == "booked" and self.status == "cancelled":
            self._refund_tickets()


class CoachAvailability(models.Model):
    STATUS_CHOICES = (
        ("available", "Available"),
        ("unavailable", "Unavailable"),
    )

    coach = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="coach_availabilities",
    )
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    capacity = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="available")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-start_time"]

    def __str__(self):
        return f"{self.date} {self.start_time}-{self.end_time} ({self.coach}) cap={self.capacity}"

    @property
    def start_at(self):
        return timezone.make_aware(
            datetime.combine(self.date, self.start_time),
            timezone.get_current_timezone(),
        )

    @property
    def end_at(self):
        return timezone.make_aware(
            datetime.combine(self.date, self.end_time),
            timezone.get_current_timezone(),
        )

    @property
    def reserved_count(self) -> int:
        return Reservation.objects.filter(
            coach=self.coach,
            date=self.date,
            status="booked",
            start_time__lt=self.end_time,
            end_time__gt=self.start_time,
        ).count()

    @property
    def remaining(self) -> int:
        return max(int(self.capacity or 1) - self.reserved_count, 0)

    def clean(self):
        errors = []

        if getattr(self.coach, "role", "") != "coach":
            errors.append("coach には coach ロールのユーザーを指定してください。")

        if self.end_time <= self.start_time:
            errors.append("終了時刻は開始時刻より後にしてください。")

        if self.capacity < 1:
            errors.append("定員は1以上にしてください。")

        if self.status == "available":
            _validate_business_rules(self.date, self.start_time, self.end_time)

        qs = CoachAvailability.objects.filter(
            coach=self.coach,
            date=self.date,
            status="available",
        ).exclude(pk=self.pk)

        overlap = qs.filter(
            start_time__lt=self.end_time,
            end_time__gt=self.start_time,
        ).exists()

        if overlap and self.status == "available":
            errors.append("同じ時間帯に既に空き枠があります。")

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)
