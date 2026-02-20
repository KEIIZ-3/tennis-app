from __future__ import annotations

from datetime import datetime

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

    # ① コーチ別カラー（FullCalendar表示用）
    # coach以外にも入るが、運用上coachだけ設定すればOK
    color = models.CharField(max_length=7, default="#2ecc71")  # "#RRGGBB"


class Court(models.Model):
    name = models.CharField(max_length=50, unique=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


# ===== ③ 営業時間 / 休館日 =====
class BusinessHours(models.Model):
    """
    曜日ごとの営業時間。
    未設定曜日は「予約不可」にして事故を防ぐ（= cleanで弾く）。
    """
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
        return f"{self.get_weekday_display()} {self.open_time}-{self.close_time}" + (" (closed)" if self.is_closed else "")


class FacilityClosure(models.Model):
    """特定日の休館日"""
    date = models.DateField(unique=True)
    reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"{self.date} {self.reason}".strip()


# ===== ⑤ チケット =====
class TicketWallet(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ticket_wallet")
    balance = models.IntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user} balance={self.balance}"


class TicketTransaction(models.Model):
    REASONS = [
        ("purchase", "Purchase"),
        ("admin_adjust", "Admin adjust"),
        ("consume", "Consume"),
        ("refund", "Refund"),
    ]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ticket_transactions")
    delta = models.IntegerField()  # +増加 / -消費
    reason = models.CharField(max_length=30, choices=REASONS)
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    reservation = models.ForeignKey("Reservation", null=True, blank=True, on_delete=models.SET_NULL, related_name="ticket_transactions")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} {self.delta} ({self.reason})"


def _validate_business_rules(date, start_time, end_time):
    """営業時間・休館日チェック"""
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

    court = models.ForeignKey(Court, on_delete=models.PROTECT, related_name="reservations")
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()

    STATUS_CHOICES = (
        ("booked", "Booked"),
        ("cancelled", "Cancelled"),
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="booked")

    # ===== ⑤ チケット連動用（追加）=====
    KIND_CHOICES = (
        ("private_lesson", "Private lesson"),
        ("group_lesson", "Group lesson"),
        ("court_rental", "Court rental"),
    )
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default="private_lesson")
    tickets_used = models.PositiveIntegerField(default=1)  # コートレンタルは0推奨（フォーム側でも制御）
    note = models.CharField(max_length=400, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)  # 通知・監査用

    class Meta:
        ordering = ["-date", "-start_time"]
        constraints = [
            models.UniqueConstraint(
                fields=["court", "date", "start_time"],
                name="uniq_reservation_court_date_start",
            ),
        ]

    def clean(self):
        if self.end_time <= self.start_time:
            raise ValidationError("終了時刻は開始時刻より後にしてください。")

        # cancelledは制約ゆるめ（過去データ保全・運用のため）
        if self.status != "cancelled":
            # ③ 営業時間/休館日
            _validate_business_rules(self.date, self.start_time, self.end_time)

        # コート重複
        qs_court = Reservation.objects.filter(
            court=self.court,
            date=self.date,
            status="booked",
        ).exclude(pk=self.pk)

        overlap_court = qs_court.filter(
            start_time__lt=self.end_time,
            end_time__gt=self.start_time
        ).exists()
        if overlap_court:
            raise ValidationError("同じコート・同じ時間帯に既に予約があります。")

        # コーチ重複 + ④ コーチ空き枠(capacity)の制御（スクール寄り）
        if self.coach_id:
            qs_coach = Reservation.objects.filter(
                coach_id=self.coach_id,
                date=self.date,
                status="booked",
            ).exclude(pk=self.pk)

            overlap_coach = qs_coach.filter(
                start_time__lt=self.end_time,
                end_time__gt=self.start_time
            ).exists()

            # privateは基本1枠、groupはcapacityで複数を許容したい
            if self.status == "booked" and self.kind == "private_lesson" and overlap_coach:
                raise ValidationError("同じコーチが同じ時間帯に既に予約されています。")

            # CoachAvailabilityを要求（スクール運用：空き枠ベース）
            av_qs = CoachAvailability.objects.filter(
                coach_id=self.coach_id,
                date=self.date,
                status="available",
                start_time__lte=self.start_time,
                end_time__gte=self.end_time,
            )
            av = av_qs.order_by("start_time").first()
            if self.status == "booked" and not av:
                raise ValidationError("この時間帯のコーチ空き枠がありません。")

            if self.status == "booked" and av:
                # capacity超過チェック（group_lessonのみ）
                if self.kind == "group_lesson":
                    booked_count = Reservation.objects.filter(
                        coach_id=self.coach_id,
                        date=self.date,
                        status="booked",
                        kind="group_lesson",
                        start_time__lt=self.end_time,
                        end_time__gt=self.start_time,
                    ).exclude(pk=self.pk).count()
                    if booked_count + 1 > av.capacity:
                        raise ValidationError("このコーチ枠は満員です。")

        # ⑤ チケット：レッスンは1以上 / コートレンタルは0推奨
        if self.kind in ("private_lesson", "group_lesson"):
            if self.tickets_used < 1:
                raise ValidationError("レッスン予約は tickets_used を1以上にしてください。")
        else:
            # court_rental
            if self.tickets_used != 0:
                # “禁止”ではなく、運用事故を防ぐために軽く矯正するならフォーム側で0固定にする
                pass

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
            old_status = Reservation.objects.filter(pk=self.pk).values_list("status", flat=True).first()

        self.full_clean()
        super().save(*args, **kwargs)

        # ⑤ チケット自動処理（作成時消費 / booked→cancelledで返却）
        if is_new and self.status == "booked":
            self._consume_tickets()
        elif (old_status == "booked") and (self.status == "cancelled"):
            self._refund_tickets()

    def __str__(self):
        coach_part = f" coach={self.coach}" if self.coach_id else ""
        return f"{self.date} {self.start_time}-{self.end_time} {self.court} ({self.customer}){coach_part}"

    @property
    def start_at(self):
        return timezone.make_aware(datetime.combine(self.date, self.start_time), timezone.get_current_timezone())

    @property
    def end_at(self):
        return timezone.make_aware(datetime.combine(self.date, self.end_time), timezone.get_current_timezone())


class CoachAvailability(models.Model):
    coach = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="coach_availabilities",
    )
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()

    # ✅ 枠ごとの定員（migration 0006 に合わせる）
    capacity = models.PositiveIntegerField(default=1)

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

        if self.capacity < 1:
            raise ValidationError("定員は1以上にしてください。")

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
        return f"{self.date} {self.start_time}-{self.end_time} ({self.coach}) cap={self.capacity}"

    @property
    def start_at(self):
        return timezone.make_aware(datetime.combine(self.date, self.start_time), timezone.get_current_timezone())

    @property
    def end_at(self):
        return timezone.make_aware(datetime.combine(self.date, self.end_time), timezone.get_current_timezone())
