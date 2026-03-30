from datetime import datetime, time, timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

BUSINESS_START_HOUR = 9
BUSINESS_END_HOUR = 21
TICKET_BALANCE_MIN = -4


class User(AbstractUser):
    ROLE_CHOICES = (
        ("member", "member"),
        ("coach", "coach"),
    )

    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="member")
    full_name = models.CharField(max_length=150, blank=True, default="")
    phone_number = models.CharField(max_length=30, blank=True, default="")
    is_profile_completed = models.BooleanField(default=False)
    ticket_balance = models.IntegerField(default=0)

    def is_coach(self):
        return self.role == "coach"

    def display_name(self):
        if self.full_name:
            return self.full_name
        if self.first_name:
            return self.first_name
        return self.username

    def __str__(self):
        return self.display_name()


class Court(models.Model):
    name = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.name


class LessonTypeMixin:
    LESSON_GROUP = "group"
    LESSON_PRIVATE = "private"

    LESSON_TYPE_CHOICES = (
        (LESSON_GROUP, "一般レッスン（2時間 / 1枚）"),
        (LESSON_PRIVATE, "プライベートレッスン（1時間 / 2枚）"),
    )

    @classmethod
    def duration_hours_for_lesson_type(cls, lesson_type: str) -> int:
        if lesson_type == cls.LESSON_PRIVATE:
            return 1
        return 2

    @classmethod
    def tickets_for_lesson_type(cls, lesson_type: str) -> int:
        if lesson_type == cls.LESSON_PRIVATE:
            return 2
        return 1


class CoachAvailability(models.Model, LessonTypeMixin):
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
    lesson_type = models.CharField(
        max_length=20,
        choices=LessonTypeMixin.LESSON_TYPE_CHOICES,
        default=LessonTypeMixin.LESSON_GROUP,
    )
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    capacity = models.PositiveIntegerField(default=1)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start_at", "coach_id", "court_id"]

    def __str__(self):
        return f"{self.coach} / {self.court} / {self.get_lesson_type_display()} / {self.start_at:%Y-%m-%d %H:%M}"

    def clean(self):
        if not self.start_at or not self.end_at:
            return

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

        start_local = timezone.localtime(self.start_at) if timezone.is_aware(self.start_at) else self.start_at
        end_local = timezone.localtime(self.end_at) if timezone.is_aware(self.end_at) else self.end_at

        if start_local.hour < BUSINESS_START_HOUR or start_local.hour >= BUSINESS_END_HOUR:
            raise ValidationError("開始時刻は 09:00〜20:00 の範囲で指定してください。")

        if end_local.hour <= BUSINESS_START_HOUR or end_local.hour > BUSINESS_END_HOUR:
            raise ValidationError("終了時刻は 10:00〜21:00 の範囲で指定してください。")

        duration = self.end_at - self.start_at
        expected_duration = timedelta(hours=self.duration_hours_for_lesson_type(self.lesson_type))
        if duration != expected_duration:
            raise ValidationError("レッスン種別に応じた時間で登録してください。一般レッスンは2時間、プライベートは1時間です。")

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


class FixedLesson(models.Model, LessonTypeMixin):
    WEEKDAY_CHOICES = (
        (0, "月"),
        (1, "火"),
        (2, "水"),
        (3, "木"),
        (4, "金"),
        (5, "土"),
        (6, "日"),
    )

    title = models.CharField(max_length=150, default="", blank=True)
    coach = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="fixed_lessons_as_coach",
        limit_choices_to={"role": "coach"},
    )
    court = models.ForeignKey(
        Court,
        on_delete=models.CASCADE,
        related_name="fixed_lessons",
    )
    members = models.ManyToManyField(
        User,
        related_name="fixed_lessons",
        limit_choices_to={"role": "member"},
        blank=True,
    )
    lesson_type = models.CharField(
        max_length=20,
        choices=LessonTypeMixin.LESSON_TYPE_CHOICES,
        default=LessonTypeMixin.LESSON_GROUP,
    )
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAY_CHOICES)
    start_hour = models.PositiveSmallIntegerField(default=9)
    capacity = models.PositiveIntegerField(default=4)
    weeks_ahead = models.PositiveIntegerField(default=8)
    is_active = models.BooleanField(default=True)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["weekday", "start_hour", "id"]

    def __str__(self):
        base = self.title or f"{self.get_weekday_display()} {self.start_hour:02d}:00"
        return f"{base} / {self.coach}"

    def clean(self):
        if self.start_hour < BUSINESS_START_HOUR or self.start_hour >= BUSINESS_END_HOUR:
            raise ValidationError("固定レッスンの開始時刻は 09:00〜20:00 の範囲で指定してください。")

        duration_hours = self.duration_hours_for_lesson_type(self.lesson_type)
        if self.start_hour + duration_hours > BUSINESS_END_HOUR:
            raise ValidationError("固定レッスンの終了時刻が営業時間を超えています。")

        if self.capacity < 1:
            raise ValidationError("定員は1以上にしてください。")

    def _build_datetimes_for_date(self, target_date):
        start_dt = datetime.combine(target_date, time(self.start_hour, 0))
        if timezone.is_naive(start_dt):
            start_dt = timezone.make_aware(start_dt)
        end_dt = start_dt + timedelta(hours=self.duration_hours_for_lesson_type(self.lesson_type))
        return start_dt, end_dt

    def sync_future_reservations(self, created_by=None):
        if not self.is_active:
            return 0

        created_count = 0
        today = timezone.localdate()
        initial_offset = (self.weekday - today.weekday()) % 7
        members = list(self.members.all())
        required_capacity = max(self.capacity, len(members), 1)

        for week_index in range(self.weeks_ahead):
            target_date = today + timedelta(days=initial_offset + (7 * week_index))
            start_at, end_at = self._build_datetimes_for_date(target_date)

            availability, _ = CoachAvailability.objects.get_or_create(
                coach=self.coach,
                court=self.court,
                lesson_type=self.lesson_type,
                start_at=start_at,
                end_at=end_at,
                defaults={
                    "capacity": required_capacity,
                    "note": f"固定レッスン: {self.title or self.get_weekday_display()}",
                },
            )

            updated_fields = []
            if availability.capacity < required_capacity:
                availability.capacity = required_capacity
                updated_fields.append("capacity")
            if not availability.note:
                availability.note = f"固定レッスン: {self.title or self.get_weekday_display()}"
                updated_fields.append("note")
            if updated_fields:
                availability.save(update_fields=updated_fields)

            for member in members:
                existing = Reservation.objects.filter(
                    user=member,
                    coach=self.coach,
                    court=self.court,
                    start_at=start_at,
                    end_at=end_at,
                    fixed_lesson=self,
                ).first()
                if existing:
                    continue

                reservation = Reservation(
                    user=member,
                    coach=self.coach,
                    court=self.court,
                    availability=availability,
                    fixed_lesson=self,
                    is_fixed_entry=True,
                    lesson_type=self.lesson_type,
                    start_at=start_at,
                    end_at=end_at,
                    status=Reservation.STATUS_ACTIVE,
                )

                try:
                    with transaction.atomic():
                        reservation.full_clean()
                        reservation.save()
                        reservation.consume_tickets(
                            reason=TicketLedger.REASON_FIXED_USE,
                            created_by=created_by,
                            note=f"固定レッスン自動登録: {self.title or self.get_weekday_display()}",
                        )
                        created_count += 1
                except ValidationError:
                    continue

        return created_count


def apply_ticket_change(
    *,
    user,
    amount: int,
    reason: str,
    note: str = "",
    created_by=None,
    reservation=None,
    fixed_lesson=None,
):
    if amount == 0:
        return None

    with transaction.atomic():
        locked_user = User.objects.select_for_update().get(pk=user.pk)
        next_balance = locked_user.ticket_balance + amount

        if next_balance < TICKET_BALANCE_MIN:
            raise ValidationError(f"チケット残数の下限は {TICKET_BALANCE_MIN} 枚です。")

        locked_user.ticket_balance = next_balance
        locked_user.save(update_fields=["ticket_balance"])

        ledger = TicketLedger.objects.create(
            user=locked_user,
            reservation=reservation,
            fixed_lesson=fixed_lesson,
            change_amount=amount,
            balance_after=next_balance,
            reason=reason,
            note=note,
            created_by=created_by if created_by and getattr(created_by, "pk", None) else None,
        )

        user.ticket_balance = next_balance
        return ledger


class Reservation(models.Model, LessonTypeMixin):
    STATUS_ACTIVE = "active"
    STATUS_CANCELED = "canceled"
    STATUS_RAIN_CANCELED = "rain_canceled"

    STATUS_CHOICES = (
        (STATUS_ACTIVE, "予約中"),
        (STATUS_CANCELED, "キャンセル"),
        (STATUS_RAIN_CANCELED, "雨天中止"),
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
    fixed_lesson = models.ForeignKey(
        FixedLesson,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reservations",
    )
    is_fixed_entry = models.BooleanField(default=False)
    lesson_type = models.CharField(
        max_length=20,
        choices=LessonTypeMixin.LESSON_TYPE_CHOICES,
        default=LessonTypeMixin.LESSON_GROUP,
    )
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    tickets_used = models.PositiveIntegerField(default=0)
    ticket_consumed_at = models.DateTimeField(null=True, blank=True)
    ticket_refunded_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    canceled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-start_at", "-id"]

    def __str__(self):
        return f"{self.user} / {self.coach} / {self.get_lesson_type_display()} / {self.start_at:%Y-%m-%d %H:%M}"

    @property
    def is_active(self):
        return self.status == self.STATUS_ACTIVE

    @property
    def is_canceled(self):
        return self.status in (self.STATUS_CANCELED, self.STATUS_RAIN_CANCELED)

    def clean(self):
        if not self.start_at or not self.end_at:
            return

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

        start_local = timezone.localtime(self.start_at) if timezone.is_aware(self.start_at) else self.start_at
        end_local = timezone.localtime(self.end_at) if timezone.is_aware(self.end_at) else self.end_at

        if start_local.hour < BUSINESS_START_HOUR or start_local.hour >= BUSINESS_END_HOUR:
            raise ValidationError("予約開始時刻は 09:00〜20:00 の範囲で指定してください。")

        if end_local.hour <= BUSINESS_START_HOUR or end_local.hour > BUSINESS_END_HOUR:
            raise ValidationError("予約終了時刻は 10:00〜21:00 の範囲で指定してください。")

        expected_duration = timedelta(hours=self.duration_hours_for_lesson_type(self.lesson_type))
        if self.end_at - self.start_at != expected_duration:
            raise ValidationError("レッスン種別に応じた時間で予約してください。一般レッスンは2時間、プライベートは1時間です。")

        self.tickets_used = self.tickets_for_lesson_type(self.lesson_type)

        if self.user_id and self.coach_id and self.user_id == self.coach_id:
            raise ValidationError("自分自身を予約することはできません。")

        if self.status != self.STATUS_ACTIVE:
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

        availability = CoachAvailability.objects.filter(
            coach=self.coach,
            court=self.court,
            lesson_type=self.lesson_type,
            start_at=self.start_at,
            end_at=self.end_at,
        ).first()

        if not availability:
            raise ValidationError("該当するレッスン枠がありません。")

        self.availability = availability

        slot_reservations_qs = Reservation.objects.filter(
            coach=self.coach,
            court=self.court,
            lesson_type=self.lesson_type,
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

    def active_count_in_same_slot(self):
        return Reservation.objects.filter(
            coach=self.coach,
            court=self.court,
            lesson_type=self.lesson_type,
            start_at=self.start_at,
            end_at=self.end_at,
            status=self.STATUS_ACTIVE,
        ).count()

    def consume_tickets(self, reason="reservation_use", created_by=None, note=""):
        if self.ticket_consumed_at or self.tickets_used <= 0:
            return None

        ledger = apply_ticket_change(
            user=self.user,
            amount=-self.tickets_used,
            reason=reason,
            note=note or f"予約消費: {self.start_at:%Y-%m-%d %H:%M}",
            created_by=created_by,
            reservation=self,
            fixed_lesson=self.fixed_lesson,
        )
        self.ticket_consumed_at = timezone.now()
        self.save(update_fields=["ticket_consumed_at"])
        return ledger

    def refund_tickets(self, reason="reservation_cancel_refund", created_by=None, note=""):
        if not self.ticket_consumed_at or self.ticket_refunded_at or self.tickets_used <= 0:
            return None

        ledger = apply_ticket_change(
            user=self.user,
            amount=self.tickets_used,
            reason=reason,
            note=note or f"チケット返却: {self.start_at:%Y-%m-%d %H:%M}",
            created_by=created_by,
            reservation=self,
            fixed_lesson=self.fixed_lesson,
        )
        self.ticket_refunded_at = timezone.now()
        self.save(update_fields=["ticket_refunded_at"])
        return ledger

    def cancel(self, created_by=None, reason=""):
        if self.status != self.STATUS_ACTIVE:
            return False

        self.status = self.STATUS_CANCELED
        self.canceled_at = timezone.now()
        self.cancellation_reason = reason or "会員キャンセル"
        self.save(update_fields=["status", "canceled_at", "cancellation_reason"])
        self.refund_tickets(
            reason=TicketLedger.REASON_CANCEL_REFUND,
            created_by=created_by,
            note=f"予約キャンセル返却: {self.start_at:%Y-%m-%d %H:%M}",
        )
        return True

    def mark_rain_canceled(self, created_by=None, reason="雨天中止"):
        if self.status != self.STATUS_ACTIVE:
            return False

        self.status = self.STATUS_RAIN_CANCELED
        self.canceled_at = timezone.now()
        self.cancellation_reason = reason
        self.save(update_fields=["status", "canceled_at", "cancellation_reason"])
        self.refund_tickets(
            reason=TicketLedger.REASON_RAIN_REFUND,
            created_by=created_by,
            note=f"雨天中止返却: {self.start_at:%Y-%m-%d %H:%M}",
        )
        return True


class TicketLedger(models.Model):
    REASON_PURCHASE_SINGLE = "purchase_single"
    REASON_PURCHASE_SET4 = "purchase_set4"
    REASON_RESERVATION_USE = "reservation_use"
    REASON_FIXED_USE = "fixed_use"
    REASON_CANCEL_REFUND = "cancel_refund"
    REASON_RAIN_REFUND = "rain_refund"
    REASON_ADMIN_ADJUST = "admin_adjust"

    REASON_CHOICES = (
        (REASON_PURCHASE_SINGLE, "チケット1枚購入"),
        (REASON_PURCHASE_SET4, "4枚セット購入"),
        (REASON_RESERVATION_USE, "通常予約で消費"),
        (REASON_FIXED_USE, "固定レッスンで消費"),
        (REASON_CANCEL_REFUND, "キャンセル返却"),
        (REASON_RAIN_REFUND, "雨天中止返却"),
        (REASON_ADMIN_ADJUST, "管理画面調整"),
    )

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="ticket_ledgers",
    )
    reservation = models.ForeignKey(
        "Reservation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ticket_ledgers",
    )
    fixed_lesson = models.ForeignKey(
        "FixedLesson",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ticket_ledgers",
    )
    change_amount = models.IntegerField()
    balance_after = models.IntegerField()
    reason = models.CharField(max_length=30, choices=REASON_CHOICES)
    note = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_ticket_ledgers",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        sign = "+" if self.change_amount >= 0 else ""
        return f"{self.user} / {sign}{self.change_amount} / {self.get_reason_display()}"


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
