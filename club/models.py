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

    LEVEL_FAMILY = "family"
    LEVEL_BEGINNER = "beginner"
    LEVEL_BEGINNER_PLUS = "beginner_plus"
    LEVEL_INTERMEDIATE = "intermediate"
    LEVEL_INTERMEDIATE_PLUS = "intermediate_plus"
    LEVEL_ADVANCED = "advanced"

    LEVEL_CHOICES = (
        (LEVEL_FAMILY, "ファミリー"),
        (LEVEL_BEGINNER, "初級"),
        (LEVEL_BEGINNER_PLUS, "初中級"),
        (LEVEL_INTERMEDIATE, "中級"),
        (LEVEL_INTERMEDIATE_PLUS, "中上級"),
        (LEVEL_ADVANCED, "上級"),
    )

    LEVEL_ORDER = {
        LEVEL_FAMILY: 1,
        LEVEL_BEGINNER: 2,
        LEVEL_BEGINNER_PLUS: 3,
        LEVEL_INTERMEDIATE: 4,
        LEVEL_INTERMEDIATE_PLUS: 5,
        LEVEL_ADVANCED: 6,
    }

    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="member")
    full_name = models.CharField(max_length=150, blank=True, default="")
    phone_number = models.CharField(max_length=30, blank=True, default="")
    is_profile_completed = models.BooleanField(default=False)
    ticket_balance = models.IntegerField(default=0)
    member_level = models.CharField(
        max_length=30,
        choices=LEVEL_CHOICES,
        default=LEVEL_BEGINNER,
    )

    def is_coach(self):
        return self.role == "coach"

    def display_name(self):
        if self.full_name:
            return self.full_name
        if self.first_name:
            return self.first_name
        return self.username

    def level_rank(self):
        return self.LEVEL_ORDER.get(self.member_level, 0)

    def can_book_level(self, target_level: str) -> bool:
        return self.level_rank() >= self.LEVEL_ORDER.get(target_level, 999)

    def __str__(self):
        return self.display_name()


class Court(models.Model):
    COURT_SONO = "sono"
    COURT_OTHER = "other"

    COURT_TYPE_CHOICES = (
        (COURT_SONO, "西猪名公園テニスコート"),
        (COURT_OTHER, "それ以外のコート"),
    )

    name = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)
    court_type = models.CharField(
        max_length=20,
        choices=COURT_TYPE_CHOICES,
        default=COURT_SONO,
    )

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.name


class LessonTypeMixin:
    LESSON_GENERAL = "general"
    LESSON_PRIVATE = "private"
    LESSON_GROUP = "group"
    LESSON_EVENT = "event"

    LESSON_TYPE_CHOICES = (
        (LESSON_GENERAL, "一般レッスン"),
        (LESSON_PRIVATE, "プライベートレッスン"),
        (LESSON_GROUP, "グループレッスン"),
        (LESSON_EVENT, "イベント"),
    )

    @classmethod
    def minimum_duration_hours_for_lesson_type(cls, lesson_type: str, custom_duration_hours=None) -> int:
        if lesson_type == cls.LESSON_GENERAL:
            return 2
        if lesson_type == cls.LESSON_EVENT:
            return int(custom_duration_hours or 1)
        return 1

    @classmethod
    def is_flexible_duration_lesson_type(cls, lesson_type: str) -> bool:
        return lesson_type in (cls.LESSON_PRIVATE, cls.LESSON_GROUP)

    @classmethod
    def default_tickets_for_lesson_type(cls, lesson_type: str, custom_ticket_price=None) -> int:
        if lesson_type == cls.LESSON_PRIVATE:
            return 2
        if lesson_type == cls.LESSON_GROUP:
            return 0
        if lesson_type == cls.LESSON_EVENT:
            return int(custom_ticket_price or 0)
        return 1


class CoachAvailability(models.Model, LessonTypeMixin):
    STATUS_OPEN = "open"
    STATUS_REQUESTED = "requested"
    STATUS_APPROVED = "approved"

    STATUS_CHOICES = (
        (STATUS_OPEN, "公開中"),
        (STATUS_REQUESTED, "申請中"),
        (STATUS_APPROVED, "承認済み"),
    )

    coach = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="coach_availabilities",
        limit_choices_to={"role": "coach"},
    )
    substitute_coach = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="substitute_coach_availabilities",
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
        default=LessonTypeMixin.LESSON_GENERAL,
    )
    target_level = models.CharField(
        max_length=30,
        choices=User.LEVEL_CHOICES,
        default=User.LEVEL_BEGINNER,
    )
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    capacity = models.PositiveIntegerField(default=1)
    coach_count = models.PositiveIntegerField(default=1)
    court_count = models.PositiveIntegerField(default=1)
    note = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN)
    custom_ticket_price = models.PositiveIntegerField(default=0)
    custom_duration_hours = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start_at", "coach_id", "court_id"]

    def __str__(self):
        return f"{self.coach} / {self.court} / {self.get_lesson_type_display()} / {self.start_at:%Y-%m-%d %H:%M}"

    def duration_hours(self):
        if not self.start_at or not self.end_at:
            return 0
        delta = self.end_at - self.start_at
        return int(delta.total_seconds() // 3600)

    def effective_capacity(self):
        if self.lesson_type == self.LESSON_GENERAL:
            return max(int(self.coach_count or 1), 1) * 6
        return int(self.capacity or 0)

    def assigned_coach(self):
        return self.substitute_coach or self.coach

    def apply_substitute_to_reservations(self):
        reservation_qs = Reservation.objects.filter(
            coach=self.coach,
            court=self.court,
            lesson_type=self.lesson_type,
            start_at=self.start_at,
            end_at=self.end_at,
        )

        for reservation in reservation_qs:
            new_substitute_id = self.substitute_coach_id
            if reservation.substitute_coach_id == new_substitute_id:
                continue
            reservation.substitute_coach = self.substitute_coach
            reservation.save(update_fields=["substitute_coach"])

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

        duration_hours = self.duration_hours()
        minimum_hours = self.minimum_duration_hours_for_lesson_type(self.lesson_type, self.custom_duration_hours)

        if self.lesson_type == self.LESSON_GENERAL:
            if duration_hours != 2:
                raise ValidationError("一般レッスンは2時間で登録してください。")
        elif self.lesson_type == self.LESSON_PRIVATE:
            if duration_hours < 1:
                raise ValidationError("プライベートレッスンは1時間以上で登録してください。")
        elif self.lesson_type == self.LESSON_GROUP:
            if duration_hours < 1:
                raise ValidationError("グループレッスンは1時間以上で登録してください。")
        elif self.lesson_type == self.LESSON_EVENT:
            if duration_hours != minimum_hours:
                raise ValidationError("イベントは設定した時間で登録してください。")

        if self.substitute_coach_id and self.substitute_coach_id == self.coach_id:
            self.substitute_coach = None

        if self.lesson_type == self.LESSON_GENERAL:
            if int(self.coach_count or 0) < 1:
                raise ValidationError("一般レッスンのコーチ人数は1以上にしてください。")
            self.court_count = int(self.coach_count or 1)
            self.capacity = self.effective_capacity()

        elif self.lesson_type == self.LESSON_PRIVATE:
            self.coach_count = 1
            self.court_count = 1
            self.capacity = 1

        elif self.lesson_type == self.LESSON_GROUP:
            self.coach_count = 1
            self.court_count = 1
            if self.capacity < 2:
                raise ValidationError("グループレッスンの定員は2名以上にしてください。")

        elif self.lesson_type == self.LESSON_EVENT:
            self.coach_count = 1
            self.court_count = 1
            if self.custom_ticket_price < 0:
                raise ValidationError("イベントのチケット価格は0以上にしてください。")
            if self.custom_duration_hours < 1:
                raise ValidationError("イベントの時間は1時間以上にしてください。")
            if self.capacity < 1:
                raise ValidationError("イベントの定員は1以上にしてください。")

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
        previous_substitute_id = None
        if self.pk:
            previous_substitute_id = (
                CoachAvailability.objects.filter(pk=self.pk).values_list("substitute_coach_id", flat=True).first()
            )

        self.full_clean()
        result = super().save(*args, **kwargs)

        if previous_substitute_id != self.substitute_coach_id:
            self.apply_substitute_to_reservations()

        return result


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
        default=LessonTypeMixin.LESSON_GENERAL,
    )
    target_level = models.CharField(
        max_length=30,
        choices=User.LEVEL_CHOICES,
        default=User.LEVEL_BEGINNER,
    )
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAY_CHOICES)
    start_hour = models.PositiveSmallIntegerField(default=9)
    capacity = models.PositiveIntegerField(default=4)
    coach_count = models.PositiveIntegerField(default=1)
    court_count = models.PositiveIntegerField(default=1)
    weeks_ahead = models.PositiveIntegerField(default=8)
    is_active = models.BooleanField(default=True)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["weekday", "start_hour", "id"]

    def __str__(self):
        base = self.title or f"{self.get_weekday_display()} {self.start_hour:02d}:00"
        return f"{base} / {self.coach}"

    def effective_capacity(self):
        if self.lesson_type == self.LESSON_GENERAL:
            return max(int(self.coach_count or 1), 1) * 6
        return int(self.capacity or 0)

    def clean(self):
        if self.start_hour < BUSINESS_START_HOUR or self.start_hour >= BUSINESS_END_HOUR:
            raise ValidationError("固定レッスンの開始時刻は 09:00〜20:00 の範囲で指定してください。")

        minimum_hours = self.minimum_duration_hours_for_lesson_type(self.lesson_type)
        if self.start_hour + minimum_hours > BUSINESS_END_HOUR:
            raise ValidationError("固定レッスンの終了時刻が営業時間を超えています。")

        if self.lesson_type == self.LESSON_GENERAL:
            if int(self.coach_count or 0) < 1:
                raise ValidationError("一般レッスンのコーチ人数は1以上にしてください。")
            self.court_count = int(self.coach_count or 1)
            self.capacity = self.effective_capacity()

        elif self.lesson_type == self.LESSON_PRIVATE:
            self.coach_count = 1
            self.court_count = 1
            self.capacity = 1

        elif self.lesson_type == self.LESSON_GROUP:
            self.coach_count = 1
            self.court_count = 1
            if self.capacity < 2:
                raise ValidationError("グループレッスンの定員は2名以上にしてください。")

        elif self.lesson_type == self.LESSON_EVENT:
            self.coach_count = 1
            self.court_count = 1
            if self.capacity < 1:
                raise ValidationError("イベントの定員は1以上にしてください。")

    def _build_datetimes_for_date(self, target_date):
        start_dt = datetime.combine(target_date, time(self.start_hour, 0))
        if timezone.is_naive(start_dt):
            start_dt = timezone.make_aware(start_dt)

        if self.lesson_type == self.LESSON_GENERAL:
            end_dt = start_dt + timedelta(hours=2)
        else:
            end_dt = start_dt + timedelta(hours=1)

        return start_dt, end_dt

    def sync_future_reservations(self, created_by=None):
        if not self.is_active:
            return 0

        created_count = 0
        today = timezone.localdate()
        initial_offset = (self.weekday - today.weekday()) % 7
        members = list(self.members.all())
        required_capacity = max(self.effective_capacity(), len(members), 1)

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
                    "coach_count": self.coach_count,
                    "court_count": self.court_count,
                    "target_level": self.target_level,
                    "note": f"固定レッスン: {self.title or self.get_weekday_display()}",
                },
            )

            updated_fields = []
            if availability.capacity != required_capacity:
                availability.capacity = required_capacity
                updated_fields.append("capacity")
            if availability.coach_count != self.coach_count:
                availability.coach_count = self.coach_count
                updated_fields.append("coach_count")
            if availability.court_count != self.court_count:
                availability.court_count = self.court_count
                updated_fields.append("court_count")
            if availability.target_level != self.target_level:
                availability.target_level = self.target_level
                updated_fields.append("target_level")
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
                    substitute_coach=availability.substitute_coach,
                    court=self.court,
                    availability=availability,
                    fixed_lesson=self,
                    is_fixed_entry=True,
                    lesson_type=self.lesson_type,
                    target_level=self.target_level,
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


class TicketPurchase(models.Model):
    PURCHASE_TYPE_SINGLE = "single"
    PURCHASE_TYPE_SET4 = "set4"
    PURCHASE_TYPE_EVENT = "event"
    PURCHASE_TYPE_ADMIN = "admin"
    PURCHASE_TYPE_LEGACY = "legacy"

    PURCHASE_TYPE_CHOICES = (
        (PURCHASE_TYPE_SINGLE, "1枚購入"),
        (PURCHASE_TYPE_SET4, "4枚セット"),
        (PURCHASE_TYPE_EVENT, "イベント用"),
        (PURCHASE_TYPE_ADMIN, "管理画面調整"),
        (PURCHASE_TYPE_LEGACY, "旧データ移行"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ticket_purchases",
    )
    purchase_type = models.CharField(max_length=20, choices=PURCHASE_TYPE_CHOICES, default=PURCHASE_TYPE_SINGLE)
    total_tickets = models.PositiveIntegerField(default=0)
    remaining_tickets = models.PositiveIntegerField(default=0)
    unit_price = models.PositiveIntegerField(default=0)
    label = models.CharField(max_length=100, blank=True, default="")
    note = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_ticket_purchases",
    )
    purchased_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["purchased_at", "id"]

    def __str__(self):
        label = self.label or self.get_purchase_type_display()
        return f"{self.user} / {label} / {self.unit_price}円 / 残{self.remaining_tickets}"

    def clean(self):
        if self.total_tickets < 0:
            raise ValidationError("購入枚数は0以上にしてください。")
        if self.remaining_tickets < 0:
            raise ValidationError("残数は0以上にしてください。")
        if self.remaining_tickets > self.total_tickets:
            raise ValidationError("残数は購入枚数を超えられません。")
        if self.unit_price < 0:
            raise ValidationError("単価は0以上にしてください。")

    def unit_price_label(self):
        if self.unit_price > 0:
            return f"{self.unit_price}円券"
        return "価格不明券"


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
        settings.AUTH_USER_MODEL,
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


class TicketConsumption(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ticket_consumptions",
    )
    purchase = models.ForeignKey(
        TicketPurchase,
        on_delete=models.CASCADE,
        related_name="consumptions",
    )
    reservation = models.ForeignKey(
        "Reservation",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="ticket_consumptions",
    )
    fixed_lesson = models.ForeignKey(
        "FixedLesson",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ticket_consumptions",
    )
    tickets_used = models.PositiveIntegerField(default=1)
    unit_price_snapshot = models.PositiveIntegerField(default=0)
    refunded_at = models.DateTimeField(null=True, blank=True)
    refund_note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.user} / {self.unit_price_label()} / {self.tickets_used}枚"

    def unit_price_label(self):
        if self.unit_price_snapshot > 0:
            return f"{self.unit_price_snapshot}円券"
        return "価格不明券"

    @property
    def is_refunded(self):
        return bool(self.refunded_at)


class CoachExpense(models.Model):
    CATEGORY_COURT = "court"
    CATEGORY_BALL = "ball"
    CATEGORY_EQUIPMENT = "equipment"
    CATEGORY_SERVER = "server"
    CATEGORY_OTHER = "other"

    CATEGORY_CHOICES = (
        (CATEGORY_COURT, "コート費用"),
        (CATEGORY_BALL, "ボール費用"),
        (CATEGORY_EQUIPMENT, "テニス用機材費用"),
        (CATEGORY_SERVER, "サーバー代金"),
        (CATEGORY_OTHER, "その他"),
    )

    expense_date = models.DateField(default=timezone.localdate)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default=CATEGORY_OTHER)
    amount = models.PositiveIntegerField(default=0)
    note = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_coach_expenses",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-expense_date", "-id"]

    def __str__(self):
        return f"{self.expense_date:%Y-%m-%d} / {self.get_category_display()} / {self.amount}円"

    def clean(self):
        if self.amount < 0:
            raise ValidationError("経費は0円以上にしてください。")


class ScheduleSurveyResponse(models.Model):
    DAY_MON = "mon"
    DAY_TUE = "tue"
    DAY_WED = "wed"
    DAY_THU = "thu"
    DAY_FRI = "fri"
    DAY_SAT = "sat"
    DAY_SUN = "sun"

    DAY_CHOICES = (
        (DAY_MON, "月曜日"),
        (DAY_TUE, "火曜日"),
        (DAY_WED, "水曜日"),
        (DAY_THU, "木曜日"),
        (DAY_FRI, "金曜日"),
        (DAY_SAT, "土曜日"),
        (DAY_SUN, "日曜日"),
    )

    WEEKDAY_SLOT_09_11 = "weekday_09_11"
    WEEKDAY_SLOT_11_13 = "weekday_11_13"
    WEEKDAY_SLOT_13_15 = "weekday_13_15"
    WEEKDAY_SLOT_15_17 = "weekday_15_17"
    WEEKDAY_SLOT_17_19 = "weekday_17_19"
    WEEKDAY_SLOT_19_21 = "weekday_19_21"

    WEEKDAY_TIME_SLOT_CHOICES = (
        (WEEKDAY_SLOT_09_11, "平日 9:00〜11:00"),
        (WEEKDAY_SLOT_11_13, "平日 11:00〜13:00"),
        (WEEKDAY_SLOT_13_15, "平日 13:00〜15:00"),
        (WEEKDAY_SLOT_15_17, "平日 15:00〜17:00"),
        (WEEKDAY_SLOT_17_19, "平日 17:00〜19:00"),
        (WEEKDAY_SLOT_19_21, "平日 19:00〜21:00"),
    )

    WEEKEND_SLOT_09_11 = "weekend_09_11"
    WEEKEND_SLOT_11_13 = "weekend_11_13"
    WEEKEND_SLOT_13_15 = "weekend_13_15"
    WEEKEND_SLOT_15_17 = "weekend_15_17"
    WEEKEND_SLOT_17_19 = "weekend_17_19"
    WEEKEND_SLOT_19_21 = "weekend_19_21"

    WEEKEND_TIME_SLOT_CHOICES = (
        (WEEKEND_SLOT_09_11, "土日 9:00〜11:00"),
        (WEEKEND_SLOT_11_13, "土日 11:00〜13:00"),
        (WEEKEND_SLOT_13_15, "土日 13:00〜15:00"),
        (WEEKEND_SLOT_15_17, "土日 15:00〜17:00"),
        (WEEKEND_SLOT_17_19, "土日 17:00〜19:00"),
        (WEEKEND_SLOT_19_21, "土日 19:00〜21:00"),
    )

    LESSON_GENERAL = "general"
    LESSON_PRIVATE = "private"
    LESSON_GROUP = "group"

    LESSON_TYPE_CHOICES = (
        (LESSON_GENERAL, "一般"),
        (LESSON_PRIVATE, "プライベート"),
        (LESSON_GROUP, "グループ"),
    )

    FREQUENCY_WEEKLY_1 = "weekly_1"
    FREQUENCY_WEEKLY_2 = "weekly_2"
    FREQUENCY_MONTHLY_2_3 = "monthly_2_3"
    FREQUENCY_IRREGULAR = "irregular"

    FREQUENCY_CHOICES = (
        (FREQUENCY_WEEKLY_1, "週1回"),
        (FREQUENCY_WEEKLY_2, "週2回"),
        (FREQUENCY_MONTHLY_2_3, "月2〜3回"),
        (FREQUENCY_IRREGULAR, "不定期"),
    )

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="schedule_survey_response",
    )
    selected_days = models.JSONField(default=list, blank=True)
    selected_weekday_time_slots = models.JSONField(default=list, blank=True)
    selected_weekend_time_slots = models.JSONField(default=list, blank=True)
    selected_lesson_types = models.JSONField(default=list, blank=True)
    preferred_frequency = models.CharField(max_length=30, choices=FREQUENCY_CHOICES)
    free_comment = models.TextField(blank=True, default="")
    answered_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-answered_at", "-id"]
        verbose_name = "時間帯アンケート回答"
        verbose_name_plural = "時間帯アンケート回答"

    def __str__(self):
        return f"{self.user} / {self.answered_at:%Y-%m-%d %H:%M}"

    @classmethod
    def day_label_map(cls):
        return dict(cls.DAY_CHOICES)

    @classmethod
    def weekday_time_slot_label_map(cls):
        return dict(cls.WEEKDAY_TIME_SLOT_CHOICES)

    @classmethod
    def weekend_time_slot_label_map(cls):
        return dict(cls.WEEKEND_TIME_SLOT_CHOICES)

    @classmethod
    def lesson_type_label_map(cls):
        return dict(cls.LESSON_TYPE_CHOICES)

    @classmethod
    def frequency_label_map(cls):
        return dict(cls.FREQUENCY_CHOICES)

    def clean(self):
        allowed_days = {value for value, _label in self.DAY_CHOICES}
        allowed_weekday_slots = {value for value, _label in self.WEEKDAY_TIME_SLOT_CHOICES}
        allowed_weekend_slots = {value for value, _label in self.WEEKEND_TIME_SLOT_CHOICES}
        allowed_lesson_types = {value for value, _label in self.LESSON_TYPE_CHOICES}
        allowed_frequencies = {value for value, _label in self.FREQUENCY_CHOICES}

        self.selected_days = [value for value in (self.selected_days or []) if value in allowed_days]
        self.selected_weekday_time_slots = [
            value for value in (self.selected_weekday_time_slots or []) if value in allowed_weekday_slots
        ]
        self.selected_weekend_time_slots = [
            value for value in (self.selected_weekend_time_slots or []) if value in allowed_weekend_slots
        ]
        self.selected_lesson_types = [
            value for value in (self.selected_lesson_types or []) if value in allowed_lesson_types
        ]
        self.free_comment = (self.free_comment or "").strip()

        if not self.selected_days:
            raise ValidationError("参加しやすい曜日を1つ以上選択してください。")

        if not self.selected_weekday_time_slots and not self.selected_weekend_time_slots:
            raise ValidationError("参加しやすい時間帯を1つ以上選択してください。")

        if not self.selected_lesson_types:
            raise ValidationError("希望レッスン種別を1つ以上選択してください。")

        if self.preferred_frequency not in allowed_frequencies:
            raise ValidationError("レッスン頻度を選択してください。")

    def selected_day_labels(self):
        label_map = self.day_label_map()
        return [label_map.get(value, value) for value in (self.selected_days or [])]

    def selected_weekday_time_slot_labels(self):
        label_map = self.weekday_time_slot_label_map()
        return [label_map.get(value, value) for value in (self.selected_weekday_time_slots or [])]

    def selected_weekend_time_slot_labels(self):
        label_map = self.weekend_time_slot_label_map()
        return [label_map.get(value, value) for value in (self.selected_weekend_time_slots or [])]

    def selected_lesson_type_labels(self):
        label_map = self.lesson_type_label_map()
        return [label_map.get(value, value) for value in (self.selected_lesson_types or [])]

    def preferred_frequency_label(self):
        return self.frequency_label_map().get(self.preferred_frequency, self.preferred_frequency)


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


def purchase_tickets(
    *,
    user,
    tickets: int,
    unit_price: int,
    purchase_type: str,
    reason: str,
    note: str = "",
    created_by=None,
    reservation=None,
    fixed_lesson=None,
    purchased_at=None,
    label="",
):
    if tickets <= 0:
        raise ValidationError("購入枚数は1以上にしてください。")

    with transaction.atomic():
        ledger = apply_ticket_change(
            user=user,
            amount=tickets,
            reason=reason,
            note=note,
            created_by=created_by,
            reservation=reservation,
            fixed_lesson=fixed_lesson,
        )

        locked_user = User.objects.select_for_update().get(pk=user.pk)
        purchase = TicketPurchase.objects.create(
            user=locked_user,
            purchase_type=purchase_type,
            total_tickets=tickets,
            remaining_tickets=tickets,
            unit_price=unit_price,
            label=label,
            note=note,
            created_by=created_by if created_by and getattr(created_by, "pk", None) else None,
            purchased_at=purchased_at or timezone.now(),
        )

        user.ticket_balance = locked_user.ticket_balance
        return ledger, purchase


def _ensure_ticket_purchase_stock_for_user(user, created_by=None):
    locked_user = User.objects.select_for_update().get(pk=user.pk)
    balance = max(int(locked_user.ticket_balance or 0), 0)
    purchase_remaining = (
        TicketPurchase.objects.filter(user=locked_user).aggregate(total=models.Sum("remaining_tickets")).get("total") or 0
    )

    if purchase_remaining >= balance:
        return

    shortage = balance - purchase_remaining
    TicketPurchase.objects.create(
        user=locked_user,
        purchase_type=TicketPurchase.PURCHASE_TYPE_LEGACY,
        total_tickets=shortage,
        remaining_tickets=shortage,
        unit_price=0,
        label="旧データ移行分",
        note="既存残高との差分を補完",
        created_by=created_by if created_by and getattr(created_by, "pk", None) else None,
        purchased_at=timezone.now(),
    )


class Reservation(models.Model, LessonTypeMixin):
    STATUS_ACTIVE = "active"
    STATUS_CANCELED = "canceled"
    STATUS_RAIN_CANCELED = "rain_canceled"
    STATUS_PENDING = "pending"

    STATUS_CHOICES = (
        (STATUS_ACTIVE, "予約中"),
        (STATUS_CANCELED, "キャンセル"),
        (STATUS_RAIN_CANCELED, "雨天中止"),
        (STATUS_PENDING, "承認待ち"),
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
    substitute_coach = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="substitute_reservations",
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
        default=LessonTypeMixin.LESSON_GENERAL,
    )
    target_level = models.CharField(
        max_length=30,
        choices=User.LEVEL_CHOICES,
        default=User.LEVEL_BEGINNER,
    )
    requested_court_type = models.CharField(
        max_length=20,
        choices=Court.COURT_TYPE_CHOICES,
        default=Court.COURT_SONO,
    )
    requested_court_note = models.CharField(max_length=255, blank=True, default="")
    approved_court_note = models.CharField(max_length=255, blank=True, default="")
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    tickets_used = models.PositiveIntegerField(default=0)
    ticket_consumed_at = models.DateTimeField(null=True, blank=True)
    ticket_refunded_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    canceled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=255, blank=True, default="")
    custom_ticket_price = models.PositiveIntegerField(default=0)
    custom_duration_hours = models.PositiveIntegerField(default=0)
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

    def has_substitute_coach(self):
        return bool(self.substitute_coach_id)

    def assigned_coach(self):
        return self.substitute_coach or self.coach

    def assigned_coach_display(self):
        coach = self.assigned_coach()
        if coach:
            return coach.display_name()
        return "-"

    def normal_coach_display(self):
        if self.coach:
            return self.coach.display_name()
        return "-"

    def duration_hours(self):
        if not self.start_at or not self.end_at:
            return 0
        delta = self.end_at - self.start_at
        return int(delta.total_seconds() // 3600)

    def calculate_tickets_used(self):
        duration_hours = self.duration_hours()

        if self.lesson_type == self.LESSON_PRIVATE:
            return max(duration_hours, 1) * 2

        if self.lesson_type == self.LESSON_GROUP:
            active_count = Reservation.objects.filter(
                coach=self.coach,
                court=self.court,
                lesson_type=self.lesson_type,
                start_at=self.start_at,
                end_at=self.end_at,
                status=self.STATUS_ACTIVE,
            ).count()
            if self.pk and self.status == self.STATUS_ACTIVE:
                active_count += 1
            participant_count = max(active_count, 1)
            return max(duration_hours, 1) * participant_count

        if self.lesson_type == self.LESSON_EVENT:
            return int(self.custom_ticket_price or 0)

        return 1

    def matching_availability(self):
        return CoachAvailability.objects.filter(
            coach=self.coach,
            court=self.court,
            lesson_type=self.lesson_type,
            start_at=self.start_at,
            end_at=self.end_at,
        ).first()

    def active_slot_reservations_qs(self):
        qs = Reservation.objects.filter(
            coach=self.coach,
            court=self.court,
            lesson_type=self.lesson_type,
            start_at=self.start_at,
            end_at=self.end_at,
            status=self.STATUS_ACTIVE,
        )
        if self.pk:
            qs = qs.exclude(pk=self.pk)
        return qs

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

        duration_hours = self.duration_hours()

        if self.lesson_type == self.LESSON_GENERAL:
            if duration_hours != 2:
                raise ValidationError("一般レッスンは2時間で予約してください。")
        elif self.lesson_type == self.LESSON_PRIVATE:
            if duration_hours < 1:
                raise ValidationError("プライベートレッスンは1時間以上で予約してください。")
        elif self.lesson_type == self.LESSON_GROUP:
            if duration_hours < 1:
                raise ValidationError("グループレッスンは1時間以上で予約してください。")
        elif self.lesson_type == self.LESSON_EVENT:
            minimum_hours = self.minimum_duration_hours_for_lesson_type(self.lesson_type, self.custom_duration_hours)
            if duration_hours != minimum_hours:
                raise ValidationError("イベントは設定した時間で予約してください。")

        self.tickets_used = self.calculate_tickets_used()

        if self.user_id and self.coach_id and self.user_id == self.coach_id:
            raise ValidationError("自分自身を予約することはできません。")

        if self.substitute_coach_id and self.substitute_coach_id == self.user_id:
            raise ValidationError("会員本人を代行コーチにすることはできません。")

        if self.substitute_coach_id and self.substitute_coach_id == self.coach_id:
            self.substitute_coach = None

        if self.user and self.user.role == "member":
            if not self.user.can_book_level(self.target_level):
                raise ValidationError("ご自身のレベルでは、このレベルのレッスンは予約できません。")

        if self.status not in (self.STATUS_ACTIVE, self.STATUS_PENDING):
            return

        user_overlap_qs = Reservation.objects.filter(
            user=self.user,
            status__in=[self.STATUS_ACTIVE, self.STATUS_PENDING],
            start_at__lt=self.end_at,
            end_at__gt=self.start_at,
        )
        if self.pk:
            user_overlap_qs = user_overlap_qs.exclude(pk=self.pk)
        if user_overlap_qs.exists():
            raise ValidationError("同じ時間帯にすでに別の予約があります。")

        availability = self.matching_availability()

        if self.lesson_type in (self.LESSON_GENERAL, self.LESSON_EVENT):
            if not availability:
                raise ValidationError("該当するレッスン枠がありません。")
            self.availability = availability
            self.target_level = availability.target_level
            self.custom_ticket_price = availability.custom_ticket_price
            self.custom_duration_hours = availability.custom_duration_hours
            if availability.substitute_coach_id:
                self.substitute_coach = availability.substitute_coach

            slot_reservations_qs = self.active_slot_reservations_qs()
            if slot_reservations_qs.count() >= availability.capacity:
                raise ValidationError("この時間枠は満員です。")

        if self.lesson_type == self.LESSON_PRIVATE:
            if self.status == self.STATUS_PENDING:
                return

            if availability:
                self.availability = availability
                self.target_level = availability.target_level
                self.custom_ticket_price = availability.custom_ticket_price
                self.custom_duration_hours = availability.custom_duration_hours
                if availability.substitute_coach_id:
                    self.substitute_coach = availability.substitute_coach

            slot_reservations_qs = self.active_slot_reservations_qs()
            if slot_reservations_qs.exists():
                raise ValidationError("このプライベート枠はすでに予約済みです。")

            effective_capacity = 1
            if availability:
                effective_capacity = int(availability.capacity or 1)

            if effective_capacity < 1:
                raise ValidationError("このプライベート枠は予約できません。")

        if self.lesson_type == self.LESSON_GROUP:
            if self.status == self.STATUS_PENDING:
                if self.requested_court_type == Court.COURT_OTHER and not self.requested_court_note:
                    raise ValidationError("それ以外のコートを選択した場合は、コート情報を入力してください。")
                return

            if self.requested_court_type == Court.COURT_OTHER and not self.requested_court_note:
                raise ValidationError("それ以外のコートを選択した場合は、コート情報を入力してください。")

            if availability:
                self.availability = availability
                self.target_level = availability.target_level
                self.custom_ticket_price = availability.custom_ticket_price
                self.custom_duration_hours = availability.custom_duration_hours
                if availability.substitute_coach_id:
                    self.substitute_coach = availability.substitute_coach

                slot_reservations_qs = self.active_slot_reservations_qs()
                if slot_reservations_qs.count() >= availability.capacity:
                    raise ValidationError("このグループ枠は満員です。")

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

    def ticket_consumption_queryset(self):
        return self.ticket_consumptions.select_related("purchase").order_by("created_at", "id")

    def ticket_breakdown_items(self):
        summary = {}
        for consumption in self.ticket_consumption_queryset():
            unit_price = int(consumption.unit_price_snapshot or 0)
            summary.setdefault(unit_price, 0)
            summary[unit_price] += int(consumption.tickets_used or 0)

        items = []
        for unit_price, tickets in sorted(summary.items(), key=lambda x: (x[0],)):
            if unit_price > 0:
                label = f"{unit_price}円券"
            else:
                label = "価格不明券"
            items.append(
                {
                    "unit_price": unit_price,
                    "tickets": tickets,
                    "label": label,
                }
            )
        return items

    def ticket_breakdown_text(self):
        items = self.ticket_breakdown_items()
        if not items:
            return "-"
        return " / ".join([f"{item['label']} {item['tickets']}枚" for item in items])

    def consume_tickets(self, reason="reservation_use", created_by=None, note=""):
        if self.ticket_consumed_at or self.tickets_used <= 0:
            return None

        with transaction.atomic():
            _ensure_ticket_purchase_stock_for_user(self.user, created_by=created_by)

            locked_user = User.objects.select_for_update().get(pk=self.user.pk)
            purchases = list(
                TicketPurchase.objects.select_for_update()
                .filter(user=locked_user, remaining_tickets__gt=0)
                .order_by("purchased_at", "id")
            )

            total_remaining = sum([purchase.remaining_tickets for purchase in purchases])
            if total_remaining < self.tickets_used:
                raise ValidationError("使用可能なチケット在庫が不足しています。")

            remaining_to_consume = self.tickets_used
            for purchase in purchases:
                if remaining_to_consume <= 0:
                    break
                use_count = min(purchase.remaining_tickets, remaining_to_consume)
                if use_count <= 0:
                    continue

                purchase.remaining_tickets -= use_count
                purchase.save(update_fields=["remaining_tickets"])

                TicketConsumption.objects.create(
                    user=locked_user,
                    purchase=purchase,
                    reservation=self,
                    fixed_lesson=self.fixed_lesson,
                    tickets_used=use_count,
                    unit_price_snapshot=purchase.unit_price,
                )
                remaining_to_consume -= use_count

            ledger = apply_ticket_change(
                user=locked_user,
                amount=-self.tickets_used,
                reason=reason,
                note=note or f"予約消費: {self.start_at:%Y-%m-%d %H:%M}",
                created_by=created_by,
                reservation=self,
                fixed_lesson=self.fixed_lesson,
            )

            consumed_at = timezone.now()
            Reservation.objects.filter(pk=self.pk).update(ticket_consumed_at=consumed_at)
            self.ticket_consumed_at = consumed_at
            self.user.ticket_balance = locked_user.ticket_balance
            return ledger

    def refund_tickets(self, reason="reservation_cancel_refund", created_by=None, note=""):
        if not self.ticket_consumed_at or self.ticket_refunded_at or self.tickets_used <= 0:
            return None

        with transaction.atomic():
            consumptions = list(
                self.ticket_consumptions.select_related("purchase").select_for_update().filter(refunded_at__isnull=True)
            )

            if not consumptions:
                ledger = apply_ticket_change(
                    user=self.user,
                    amount=self.tickets_used,
                    reason=reason,
                    note=note or f"チケット返却: {self.start_at:%Y-%m-%d %H:%M}",
                    created_by=created_by,
                    reservation=self,
                    fixed_lesson=self.fixed_lesson,
                )
                refunded_at = timezone.now()
                Reservation.objects.filter(pk=self.pk).update(ticket_refunded_at=refunded_at)
                self.ticket_refunded_at = refunded_at
                return ledger

            for consumption in consumptions:
                purchase = consumption.purchase
                purchase.remaining_tickets += consumption.tickets_used
                if purchase.remaining_tickets > purchase.total_tickets:
                    purchase.remaining_tickets = purchase.total_tickets
                purchase.save(update_fields=["remaining_tickets"])

                consumption.refunded_at = timezone.now()
                consumption.refund_note = note or "予約返却"
                consumption.save(update_fields=["refunded_at", "refund_note"])

            ledger = apply_ticket_change(
                user=self.user,
                amount=self.tickets_used,
                reason=reason,
                note=note or f"チケット返却: {self.start_at:%Y-%m-%d %H:%M}",
                created_by=created_by,
                reservation=self,
                fixed_lesson=self.fixed_lesson,
            )

            refunded_at = timezone.now()
            Reservation.objects.filter(pk=self.pk).update(ticket_refunded_at=refunded_at)
            self.ticket_refunded_at = refunded_at
            return ledger

    def activate_after_approval(self, created_by=None, approved_note=""):
        if self.status != self.STATUS_PENDING:
            raise ValidationError("承認待ちの申請のみ承認できます。")

        if self.lesson_type not in (self.LESSON_PRIVATE, self.LESSON_GROUP):
            raise ValidationError("承認処理の対象外のレッスン種別です。")

        with transaction.atomic():
            locked_self = Reservation.objects.select_for_update().get(pk=self.pk)
            locked_self.status = self.STATUS_ACTIVE
            locked_self.canceled_at = None
            locked_self.cancellation_reason = ""

            if approved_note:
                locked_self.approved_court_note = approved_note

            locked_self.tickets_used = locked_self.calculate_tickets_used()
            locked_self.full_clean()
            locked_self.save()

            locked_self.consume_tickets(
                reason=TicketLedger.REASON_RESERVATION_USE,
                created_by=created_by,
                note=f"承認済み予約のチケット消費: {locked_self.start_at:%Y-%m-%d %H:%M}",
            )

            self.status = locked_self.status
            self.tickets_used = locked_self.tickets_used
            self.canceled_at = locked_self.canceled_at
            self.cancellation_reason = locked_self.cancellation_reason
            self.approved_court_note = locked_self.approved_court_note
            self.availability = locked_self.availability
            self.substitute_coach = locked_self.substitute_coach
            self.custom_ticket_price = locked_self.custom_ticket_price
            self.custom_duration_hours = locked_self.custom_duration_hours
            self.ticket_consumed_at = locked_self.ticket_consumed_at

        return True

    def reject_request(self, created_by=None, reason="コーチ却下"):
        if self.status != self.STATUS_PENDING:
            raise ValidationError("承認待ちの申請のみ却下できます。")

        canceled_at = timezone.now()
        Reservation.objects.filter(pk=self.pk, status=self.STATUS_PENDING).update(
            status=self.STATUS_CANCELED,
            canceled_at=canceled_at,
            cancellation_reason=reason or "コーチ却下",
        )
        self.status = self.STATUS_CANCELED
        self.canceled_at = canceled_at
        self.cancellation_reason = reason or "コーチ却下"
        return True

    def cancel(self, created_by=None, reason=""):
        if self.status not in (self.STATUS_ACTIVE, self.STATUS_PENDING):
            return False

        canceled_at = timezone.now()
        Reservation.objects.filter(pk=self.pk).update(
            status=self.STATUS_CANCELED,
            canceled_at=canceled_at,
            cancellation_reason=reason or "会員キャンセル",
        )
        self.status = self.STATUS_CANCELED
        self.canceled_at = canceled_at
        self.cancellation_reason = reason or "会員キャンセル"

        self.refund_tickets(
            reason=TicketLedger.REASON_CANCEL_REFUND,
            created_by=created_by,
            note=f"予約キャンセル返却: {self.start_at:%Y-%m-%d %H:%M}",
        )
        return True

    def mark_rain_canceled(self, created_by=None, reason="雨天中止"):
        if self.status != self.STATUS_ACTIVE:
            return False

        canceled_at = timezone.now()
        Reservation.objects.filter(pk=self.pk).update(
            status=self.STATUS_RAIN_CANCELED,
            canceled_at=canceled_at,
            cancellation_reason=reason,
        )
        self.status = self.STATUS_RAIN_CANCELED
        self.canceled_at = canceled_at
        self.cancellation_reason = reason

        self.refund_tickets(
            reason=TicketLedger.REASON_RAIN_REFUND,
            created_by=created_by,
            note=f"雨天中止返却: {self.start_at:%Y-%m-%d %H:%M}",
        )
        return True




class StringingOrder(models.Model):
    STATUS_REQUESTED = "requested"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELED = "canceled"

    STATUS_CHOICES = (
        (STATUS_REQUESTED, "受付済み"),
        (STATUS_IN_PROGRESS, "対応中"),
        (STATUS_COMPLETED, "完了"),
        (STATUS_CANCELED, "キャンセル"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="stringing_orders",
    )
    assigned_coach = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_stringing_orders",
        limit_choices_to={"role": "coach"},
    )
    racket_name = models.CharField(max_length=120, blank=True, default="")
    string_name = models.CharField(max_length=120, blank=True, default="")
    delivery_requested = models.BooleanField(default=False)
    delivery_location = models.CharField(max_length=255, blank=True, default="")
    preferred_delivery_time = models.CharField(max_length=255, blank=True, default="")
    note = models.TextField(blank=True, default="")
    base_price = models.PositiveIntegerField(default=1200)
    delivery_fee = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_REQUESTED)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "ガット貼り依頼"
        verbose_name_plural = "ガット貼り依頼"

    def __str__(self):
        return f"{self.user} / ガット貼り / {self.created_at:%Y-%m-%d %H:%M}"

    def clean(self):
        self.racket_name = (self.racket_name or "").strip()
        self.string_name = (self.string_name or "").strip()
        self.delivery_location = (self.delivery_location or "").strip()
        self.preferred_delivery_time = (self.preferred_delivery_time or "").strip()
        self.note = (self.note or "").strip()

        if self.base_price < 0:
            raise ValidationError("基本料金は0円以上にしてください。")

        if self.delivery_requested:
            self.delivery_fee = 500
            if not self.delivery_location:
                raise ValidationError("デリバリー希望の場合は、届け場所を入力してください。")
            if not self.preferred_delivery_time:
                raise ValidationError("デリバリー希望の場合は、時間指定を入力してください。")
        else:
            self.delivery_fee = 0
            self.delivery_location = ""
            self.preferred_delivery_time = ""

    def assigned_coach_display(self):
        if self.assigned_coach:
            return self.assigned_coach.display_name()
        return "-"

    def total_price(self):
        return int(self.base_price or 0) + int(self.delivery_fee or 0)


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
