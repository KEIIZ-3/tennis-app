from datetime import date, datetime, timedelta
import uuid

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.utils import timezone

from .capacity_policy import general_lesson_capacity

from .models import (
    STRINGING_BASE_PRICE,
    STRINGING_DELIVERY_FEE,
    CoachAvailability,
    Court,
    LineAccountLink,
    Reservation,
    StringingOrder,
    TicketPurchase,
)

User = get_user_model()

BUSINESS_START_HOUR = 9
BUSINESS_END_HOUR = 21

START_HOUR_CHOICES = [(str(h), f"{h:02d}:00") for h in range(BUSINESS_START_HOUR, BUSINESS_END_HOUR)]
END_HOUR_CHOICES = [(str(h), f"{h:02d}:00") for h in range(BUSINESS_START_HOUR + 1, BUSINESS_END_HOUR + 1)]
TENSION_CHOICES = [(str(value), f"{value} lbs") for value in range(30, 61)]


class LoginForm(forms.Form):
    username = forms.CharField(label="ユーザー名", max_length=150)
    password = forms.CharField(label="パスワード", widget=forms.PasswordInput)


class TicketPurchaseCorrectionForm(forms.Form):
    tickets = forms.IntegerField(label="チケット枚数", min_value=1)
    unit_price = forms.IntegerField(label="単価", min_value=0)
    purchase_type = forms.ChoiceField(
        label="購入種別", choices=TicketPurchase.PURCHASE_TYPE_CHOICES
    )
    purchased_at = forms.DateTimeField(
        label="購入日時／受領日",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        input_formats=["%Y-%m-%dT%H:%M"],
    )
    note = forms.CharField(label="メモ", max_length=255, required=False)
    correction_reason = forms.CharField(
        label="修正理由", max_length=255, widget=forms.Textarea(attrs={"rows": 3})
    )
    cash_mode = forms.ChoiceField(
        label="現金受領記録",
        choices=(("none", "受領記録なし"), ("preserve", "金額・受領日を維持"), ("replace", "明示した内容へ変更")),
    )
    cash_amount = forms.IntegerField(label="修正後の現金受領額", min_value=1, required=False)
    cash_received_at = forms.DateTimeField(
        label="修正後の現金受領日",
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        input_formats=["%Y-%m-%dT%H:%M"],
    )
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, purchase, **kwargs):
        super().__init__(*args, **kwargs)
        self.purchase = purchase
        receipt = purchase.cash_receipts.filter(reversed_at__isnull=True).first()
        if receipt is None:
            self.fields["cash_mode"].choices = (("none", "受領記録なし"),)

    def clean(self):
        cleaned = super().clean()
        receipt = self.purchase.cash_receipts.filter(reversed_at__isnull=True).first()
        mode = cleaned.get("cash_mode")
        if receipt and mode == "replace":
            if cleaned.get("cash_amount") is None or cleaned.get("cash_received_at") is None:
                raise forms.ValidationError("現金受領額を変更する場合は、金額と受領日を明示してください。")
        if receipt and mode == "preserve":
            cleaned["cash_amount"] = receipt.amount
            cleaned["cash_received_at"] = receipt.received_at
        return cleaned


class MemberRegistrationForm(UserCreationForm):
    full_name = forms.CharField(label="お名前", max_length=150, required=True)
    email = forms.EmailField(label="メールアドレス", required=True)
    phone_number = forms.CharField(label="電話番号", max_length=30, required=True)
    member_level = forms.ChoiceField(label="レベル", choices=User.LEVEL_CHOICES, required=True)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("full_name", "username", "email", "phone_number", "member_level", "password1", "password2")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = "ユーザー名"
        self.fields["password1"].label = "パスワード"
        self.fields["password2"].label = "パスワード（確認）"
        self.fields["full_name"].widget.attrs.update({"placeholder": "例: 山田 太郎"})
        self.fields["username"].widget.attrs.update({"placeholder": "半角英数字で入力"})
        self.fields["email"].widget.attrs.update({"placeholder": "example@example.com"})
        self.fields["phone_number"].widget.attrs.update({"placeholder": "例: 09012345678"})

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip()
        if not email:
            raise forms.ValidationError("メールアドレスを入力してください。")
        qs = User.objects.filter(email__iexact=email)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("このメールアドレスはすでに登録されています。")
        return email

    def clean_phone_number(self):
        phone_number = (self.cleaned_data.get("phone_number") or "").strip()
        if not phone_number:
            raise forms.ValidationError("電話番号を入力してください。")
        return phone_number

    def save(self, commit=True):
        user = super().save(commit=False)
        user.full_name = (self.cleaned_data.get("full_name") or "").strip()
        user.first_name = user.full_name
        user.email = (self.cleaned_data.get("email") or "").strip()
        user.phone_number = (self.cleaned_data.get("phone_number") or "").strip()
        user.member_level = self.cleaned_data.get("member_level") or User.LEVEL_BEGINNER
        user.is_profile_completed = True
        if hasattr(user, "role"):
            user.role = "member"
        if commit:
            user.save()
        return user


class LineProfileCompletionForm(forms.ModelForm):
    full_name = forms.CharField(label="お名前", max_length=150, required=True)
    email = forms.EmailField(label="メールアドレス", required=True)
    phone_number = forms.CharField(label="電話番号", max_length=30, required=True)
    member_level = forms.ChoiceField(label="レベル", choices=User.LEVEL_CHOICES, required=True)

    class Meta:
        model = User
        fields = ("full_name", "email", "phone_number", "member_level")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["full_name"].widget.attrs.update({"placeholder": "例: 山田 太郎"})
        self.fields["email"].widget.attrs.update({"placeholder": "example@example.com"})
        self.fields["phone_number"].widget.attrs.update({"placeholder": "例: 09012345678"})

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip()
        if not email:
            raise forms.ValidationError("メールアドレスを入力してください。")
        qs = User.objects.filter(email__iexact=email)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("このメールアドレスはすでに登録されています。")
        return email

    def clean_phone_number(self):
        phone_number = (self.cleaned_data.get("phone_number") or "").strip()
        if not phone_number:
            raise forms.ValidationError("電話番号を入力してください。")
        return phone_number

    def save(self, commit=True):
        user = super().save(commit=False)
        user.full_name = (self.cleaned_data.get("full_name") or "").strip()
        user.first_name = user.full_name
        user.email = (self.cleaned_data.get("email") or "").strip()
        user.phone_number = (self.cleaned_data.get("phone_number") or "").strip()
        user.member_level = self.cleaned_data.get("member_level") or User.LEVEL_BEGINNER
        user.is_profile_completed = True
        if commit:
            user.save()
        return user


class CoachAvailabilityForm(forms.ModelForm):
    start_date = forms.DateField(label="開始日", widget=forms.DateInput(attrs={"type": "date"}))
    start_hour = forms.ChoiceField(label="開始時間", choices=START_HOUR_CHOICES)
    end_date = forms.DateField(label="終了日", widget=forms.DateInput(attrs={"type": "date"}))
    end_hour = forms.ChoiceField(label="終了時間", choices=END_HOUR_CHOICES)

    class Meta:
        model = CoachAvailability
        fields = [
            "coach",
            "coach_2",
            "substitute_coach",
            "court",
            "lesson_type",
            "target_level",
            "target_level_2",
            "coach_count",
            "court_count",
            "capacity",
            "custom_ticket_price",
            "custom_duration_hours",
            "note",
        ]
        widgets = {
            "coach_count": forms.NumberInput(attrs={"min": 1}),
            "court_count": forms.NumberInput(attrs={"min": 1}),
            "capacity": forms.NumberInput(attrs={"min": 1}),
            "custom_ticket_price": forms.NumberInput(attrs={"min": 0}),
            "custom_duration_hours": forms.NumberInput(attrs={"min": 0}),
            "note": forms.TextInput(attrs={"placeholder": "任意メモ"}),
        }

    def __init__(self, *args, **kwargs):
        self.request_user = kwargs.pop("request_user", None)
        super().__init__(*args, **kwargs)
        coach_queryset = User.objects.filter(role__in=User.COACH_ROLE_VALUES).order_by("username", "id")
        self.fields["coach"].queryset = coach_queryset
        self.fields["coach_2"].queryset = coach_queryset
        self.fields["coach_2"].required = False
        self.fields["coach_2"].label = "担当コーチ2"
        for field_name in ("coach_count", "court_count", "capacity"):
            self.fields[field_name].widget.attrs["readonly"] = True
        self.fields["substitute_coach"].queryset = coach_queryset
        self.fields["substitute_coach"].required = False
        self.fields["court"].queryset = Court.objects.filter(is_active=True).order_by("name")
        self.fields["coach"].label = "担当コーチ1"
        self.fields["substitute_coach"].label = "代行コーチ（その日だけ）"
        self.fields["lesson_type"].label = "レッスン種別"
        self.fields["target_level"].label = "対象レベル"
        self.fields["target_level_2"].label = "第2対象レベル"
        self.fields["target_level_2"].required = False
        self.fields["coach_count"].label = "担当コーチ人数"
        self.fields["court_count"].label = "利用コート面数"
        self.fields["capacity"].label = "定員"
        self.fields["custom_ticket_price"].label = "イベント用チケット価格"
        self.fields["custom_duration_hours"].label = "イベント用時間（時間）"
        self.fields["lesson_type"].initial = CoachAvailability.LESSON_GENERAL
        self.fields["substitute_coach"].help_text = "その日のみ代行するコーチを設定できます。未設定なら通常担当のままです。"
        self.fields["coach_count"].help_text = "一般レッスンは担当コーチ1名につきコート1面・定員5名です。2名選択時は2面・10名になります。"
        self.fields["court_count"].help_text = "一般レッスンではコーチ人数に合わせて自動調整されます。"
        self.fields["capacity"].help_text = "一般レッスンではコーチ人数から自動計算されます。"
        self.fields["custom_duration_hours"].help_text = "イベントのみ使用します。"

        if (
            self.request_user
            and not self.request_user.is_superuser
            and not self.request_user.is_staff
            and getattr(self.request_user, "role", "") in User.COACH_ROLE_VALUES
        ):
            self.fields["coach"].queryset = User.objects.filter(pk=self.request_user.pk)
            self.fields["coach"].initial = self.request_user

        start_at = self.initial.get("start_at") or getattr(self.instance, "start_at", None)
        end_at = self.initial.get("end_at") or getattr(self.instance, "end_at", None)
        policy_date = self.data.get("start_date") if self.is_bound else self.initial.get("start_date")
        try:
            policy_date = date.fromisoformat(str(policy_date))
        except (TypeError, ValueError):
            policy_date = timezone.localdate()
        self.fields["coach_2"].widget.attrs.update({
            "data-capacity-one": general_lesson_capacity(1, policy_date),
            "data-capacity-two": general_lesson_capacity(2, policy_date),
        })

        if start_at:
            if timezone.is_aware(start_at):
                start_at = timezone.localtime(start_at)
            self.fields["start_date"].initial = start_at.date()
            self.fields["start_hour"].initial = str(start_at.hour)

        if end_at:
            if timezone.is_aware(end_at):
                end_at = timezone.localtime(end_at)
            self.fields["end_date"].initial = end_at.date()
            self.fields["end_hour"].initial = str(end_at.hour)

    def _build_aware_datetime(self, date_value, hour_value):
        dt = datetime(
            year=date_value.year,
            month=date_value.month,
            day=date_value.day,
            hour=int(hour_value),
            minute=0,
            second=0,
            microsecond=0,
        )
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt)
        return dt

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get("start_date")
        start_hour = cleaned_data.get("start_hour")
        end_date = cleaned_data.get("end_date")
        end_hour = cleaned_data.get("end_hour")
        lesson_type = cleaned_data.get("lesson_type") or Reservation.LESSON_GENERAL
        custom_duration_hours = cleaned_data.get("custom_duration_hours") or 0
        coach_count = int(cleaned_data.get("coach_count") or 1)
        coach = cleaned_data.get("coach")
        coach_2 = cleaned_data.get("coach_2")
        substitute_coach = cleaned_data.get("substitute_coach")

        if self.instance._state.adding and start_date and start_date < timezone.localdate():
            self.add_error("start_date", "過去の日付には新しい単発レッスンを登録できません。")

        if not start_date or start_hour in (None, ""):
            self.add_error("start_date", "開始日時を入力してください。")
            return cleaned_data
        if not end_date or end_hour in (None, ""):
            self.add_error("end_date", "終了日時を入力してください。")
            return cleaned_data

        start_at = self._build_aware_datetime(start_date, start_hour)
        end_at = self._build_aware_datetime(end_date, end_hour)

        if start_at.hour < BUSINESS_START_HOUR or start_at.hour >= BUSINESS_END_HOUR:
            self.add_error("start_hour", "開始時刻は 09:00〜20:00 の範囲で指定してください。")
        if end_at.hour <= BUSINESS_START_HOUR or end_at.hour > BUSINESS_END_HOUR:
            self.add_error("end_hour", "終了時刻は 10:00〜21:00 の範囲で指定してください。")

        duration_hours = int((end_at - start_at).total_seconds() // 3600)

        if lesson_type == Reservation.LESSON_GENERAL and duration_hours != 2:
            raise forms.ValidationError("一般レッスンは2時間で登録してください。")
        elif lesson_type == Reservation.LESSON_PRIVATE and duration_hours < 1:
            raise forms.ValidationError("プライベートレッスンは1時間以上で登録してください。")
        elif lesson_type == Reservation.LESSON_GROUP and duration_hours < 1:
            raise forms.ValidationError("グループレッスンは1時間以上で登録してください。")
        elif lesson_type == Reservation.LESSON_EVENT:
            expected_hours = int(custom_duration_hours or 1)
            if duration_hours != expected_hours:
                raise forms.ValidationError("イベントは設定した時間で登録してください。")

        if coach and substitute_coach and coach.pk == substitute_coach.pk:
            cleaned_data["substitute_coach"] = None

        if lesson_type == Reservation.LESSON_GENERAL:
            coach_count = 2 if coach_2 else 1
            cleaned_data["coach_count"] = coach_count
            if coach_count < 1:
                self.add_error("coach_count", "一般レッスンの担当コーチ人数は1以上にしてください。")
            cleaned_data["court_count"] = coach_count
            cleaned_data["capacity"] = general_lesson_capacity(coach_count, start_at)
        elif lesson_type == Reservation.LESSON_PRIVATE:
            cleaned_data["coach_count"] = 1
            cleaned_data["court_count"] = 1
            cleaned_data["capacity"] = 1
        elif lesson_type == Reservation.LESSON_GROUP:
            cleaned_data["coach_count"] = 1
            cleaned_data["court_count"] = 1
            if int(cleaned_data.get("capacity") or 0) < 2:
                self.add_error("capacity", "グループレッスンの定員は2名以上にしてください。")
        elif lesson_type == Reservation.LESSON_EVENT:
            cleaned_data["coach_count"] = 1
            cleaned_data["court_count"] = 1

        cleaned_data["start_at"] = start_at
        cleaned_data["end_at"] = end_at
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.start_at = self.cleaned_data["start_at"]
        instance.end_at = self.cleaned_data["end_at"]
        instance.coach_count = self.cleaned_data.get("coach_count") or 1
        instance.court_count = self.cleaned_data.get("court_count") or 1
        instance.capacity = self.cleaned_data.get("capacity") or instance.capacity
        instance.substitute_coach = self.cleaned_data.get("substitute_coach")
        instance.coach_2 = self.cleaned_data.get("coach_2")
        if commit:
            instance.save()
        return instance


class ReservationCreateForm(forms.ModelForm):
    requested_court_note = forms.CharField(
        label="実施するテニスコート",
        max_length=255,
        required=False,
        help_text="Courtマスタにない場所も自由に入力できます。",
    )
    coach_choice = forms.ChoiceField(label="コーチ", required=False)
    start_date = forms.DateField(label="開始日", widget=forms.DateInput(attrs={"type": "date"}))
    start_hour = forms.ChoiceField(label="開始時間", choices=START_HOUR_CHOICES)
    end_date = forms.DateField(label="終了日", widget=forms.DateInput(attrs={"type": "date"}))
    end_hour = forms.ChoiceField(label="終了時間", choices=END_HOUR_CHOICES)

    class Meta:
        model = Reservation
        fields = ["lesson_type", "requested_court_note"]

    def __init__(self, *args, **kwargs):
        self.request_user = kwargs.pop("request_user", None)
        self.private_only = kwargs.pop("private_only", False)
        super().__init__(*args, **kwargs)

        coach_queryset = User.objects.filter(role__in=User.COACH_ROLE_VALUES).order_by("username", "id")
        self.fields["coach_choice"].choices = [("", "おまかせ")] + [
            (str(coach.pk), coach.display_name()) for coach in coach_queryset
        ]
        self.fields["coach_choice"].help_text = "指定しない場合は、空いているコーチから自動で候補を割り当てます。"
        self.fields["lesson_type"].label = "レッスン種別"
        self.fields["lesson_type"].choices = [
            (Reservation.LESSON_PRIVATE, "プライベートレッスン"),
            (Reservation.LESSON_GROUP, "グループレッスン"),
        ]
        self.fields["lesson_type"].initial = Reservation.LESSON_PRIVATE
        if self.private_only:
            self.fields["lesson_type"].choices = [
                (Reservation.LESSON_PRIVATE, "プライベートレッスン"),
            ]
            self.fields["coach_choice"].required = True
        self.fields["lesson_type"].help_text = "予約作成画面では、プライベート / グループのみ受け付けます。"

        start_at = self.initial.get("start_at") or getattr(self.instance, "start_at", None)
        end_at = self.initial.get("end_at") or getattr(self.instance, "end_at", None)
        lesson_type = self.initial.get("lesson_type") or getattr(self.instance, "lesson_type", None)
        coach_choice = self.initial.get("coach_choice") or ""

        self.fields["coach_choice"].initial = str(coach_choice)

        if lesson_type in (Reservation.LESSON_PRIVATE, Reservation.LESSON_GROUP):
            self.fields["lesson_type"].initial = lesson_type

        if start_at:
            if timezone.is_aware(start_at):
                start_at = timezone.localtime(start_at)
            self.fields["start_date"].initial = start_at.date()
            self.fields["start_hour"].initial = str(start_at.hour)

        if end_at:
            if timezone.is_aware(end_at):
                end_at = timezone.localtime(end_at)
            self.fields["end_date"].initial = end_at.date()
            self.fields["end_hour"].initial = str(end_at.hour)

        if self.private_only:
            self.fields.pop("end_date")

    def _build_aware_datetime(self, date_value, hour_value):
        dt = datetime(
            year=date_value.year,
            month=date_value.month,
            day=date_value.day,
            hour=int(hour_value),
            minute=0,
            second=0,
            microsecond=0,
        )
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt)
        return dt

    def clean(self):
        cleaned_data = super().clean()
        lesson_type = cleaned_data.get("lesson_type") or Reservation.LESSON_PRIVATE
        start_date = cleaned_data.get("start_date")
        start_hour = cleaned_data.get("start_hour")
        end_date = start_date if self.private_only else cleaned_data.get("end_date")
        end_hour = cleaned_data.get("end_hour")
        coach_choice = (cleaned_data.get("coach_choice") or "").strip()
        requested_court_note = (cleaned_data.get("requested_court_note") or "").strip()

        if lesson_type not in (Reservation.LESSON_PRIVATE, Reservation.LESSON_GROUP):
            self.add_error("lesson_type", "この画面ではプライベートまたはグループを選択してください。")

        if not start_date or start_hour in (None, ""):
            self.add_error("start_date", "開始日時を入力してください。")
            return cleaned_data
        if not end_date or end_hour in (None, ""):
            self.add_error("end_hour" if self.private_only else "end_date", "終了日時を入力してください。")
            return cleaned_data

        start_at = self._build_aware_datetime(start_date, start_hour)
        end_at = self._build_aware_datetime(end_date, end_hour)

        if start_at.hour < BUSINESS_START_HOUR or start_at.hour >= BUSINESS_END_HOUR:
            self.add_error("start_hour", "開始時刻は 09:00〜20:00 の範囲で指定してください。")
        if end_at.hour <= BUSINESS_START_HOUR or end_at.hour > BUSINESS_END_HOUR:
            self.add_error("end_hour", "終了時刻は 10:00〜21:00 の範囲で指定してください。")
        if end_at <= start_at:
            raise forms.ValidationError("終了日時は開始日時より後にしてください。")

        duration_hours = int((end_at - start_at).total_seconds() // 3600)
        if duration_hours < 1:
            raise forms.ValidationError("予約は1時間以上で指定してください。")

        if self.private_only and lesson_type != Reservation.LESSON_PRIVATE:
            self.add_error("lesson_type", "プライベートレッスンを選択してください。")

        if lesson_type == Reservation.LESSON_PRIVATE and not requested_court_note:
            self.add_error("requested_court_note", "実施するテニスコートを入力してください。")

        if coach_choice and not User.objects.filter(role__in=User.COACH_ROLE_VALUES, pk=coach_choice).exists():
            self.add_error("coach_choice", "選択されたコーチが見つかりません。")

        cleaned_data["start_at"] = start_at
        cleaned_data["end_at"] = end_at
        cleaned_data["coach_choice"] = coach_choice
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.start_at = self.cleaned_data["start_at"]
        instance.end_at = self.cleaned_data["end_at"]
        if commit:
            instance.save()
        return instance


class StringingOrderForm(forms.ModelForm):
    DELIVERY_CHOICES = (
        ("0", "デリバリーなし"),
        ("1", "デリバリー希望（+500円）"),
    )

    delivery_option = forms.ChoiceField(
        label="デリバリー",
        choices=DELIVERY_CHOICES,
        initial="0",
    )
    tension_lbs = forms.ChoiceField(
        label="張り上げテンション",
        choices=TENSION_CHOICES,
        initial="50",
    )
    preferred_finish_date = forms.DateField(
        label="希望張り上げ納期",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )

    class Meta:
        model = StringingOrder
        fields = [
            "racket_name",
            "string_name",
            "tension_lbs",
            "delivery_location",
            "preferred_delivery_time",
            "note",
        ]
        widgets = {
            "racket_name": forms.TextInput(attrs={"placeholder": "例: Ezone 100"}),
            "string_name": forms.TextInput(attrs={"placeholder": "例: ポリツアープロ 125"}),
            "delivery_location": forms.TextInput(attrs={"placeholder": "例: 西猪名公園テニスコート入口"}),
            "preferred_delivery_time": forms.TextInput(attrs={"placeholder": "例: 4/10 18:00〜19:00"}),
            "note": forms.Textarea(attrs={"placeholder": "その他要望があれば入力してください。", "rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["racket_name"].label = "ラケット名"
        self.fields["string_name"].label = "ガット名"
        self.fields["delivery_location"].label = "届け場所"
        self.fields["preferred_delivery_time"].label = "日時指定"
        self.fields["preferred_finish_date"].label = "希望張り上げ納期"
        self.fields["note"].label = "備考"

        self.fields["racket_name"].required = False
        self.fields["string_name"].required = False
        self.fields["delivery_location"].required = False
        self.fields["preferred_delivery_time"].required = False
        self.fields["preferred_finish_date"].required = False
        self.fields["note"].required = False

        if self.instance and getattr(self.instance, "pk", None):
            is_delivery = bool(self.instance.delivery_requested)
            self.fields["delivery_option"].initial = "1" if is_delivery else "0"
            self.fields["tension_lbs"].initial = str(getattr(self.instance, "tension_lbs", 50) or 50)
            if is_delivery:
                self.fields["preferred_delivery_time"].initial = self.instance.preferred_delivery_time
            elif self.instance.preferred_delivery_time:
                try:
                    self.fields["preferred_finish_date"].initial = datetime.strptime(
                        self.instance.preferred_delivery_time, "%Y-%m-%d"
                    ).date()
                except Exception:
                    self.fields["preferred_finish_date"].initial = timezone.localdate() + timedelta(days=7)
        else:
            self.fields["preferred_finish_date"].initial = timezone.localdate() + timedelta(days=7)
            self.fields["tension_lbs"].initial = "50"

        self.fields["delivery_option"].help_text = "デリバリー希望の場合のみ、届け場所と日時指定を入力してください。"
        self.fields["delivery_location"].help_text = "デリバリー希望の場合のみ入力してください。"
        self.fields["preferred_delivery_time"].help_text = "デリバリー希望の場合のみ入力してください。"
        self.fields["preferred_finish_date"].help_text = "デリバリーなしの場合のみ入力してください。初期値は依頼日から1週間後です。"
        self.fields["racket_name"].help_text = "任意です。分かる範囲で入力してください。"
        self.fields["string_name"].help_text = "任意です。希望ガットがあれば入力してください。"
        self.fields["tension_lbs"].help_text = "30〜60 lbs の範囲で選択してください。初期値は 50 lbs です。"

    def clean(self):
        cleaned_data = super().clean()
        delivery_option = str(cleaned_data.get("delivery_option") or "0")
        delivery_requested = delivery_option == "1"
        delivery_location = (cleaned_data.get("delivery_location") or "").strip()
        preferred_delivery_time = (cleaned_data.get("preferred_delivery_time") or "").strip()
        preferred_finish_date = cleaned_data.get("preferred_finish_date")
        tension_lbs = int(cleaned_data.get("tension_lbs") or 50)

        cleaned_data["delivery_requested"] = delivery_requested
        cleaned_data["delivery_location"] = delivery_location
        cleaned_data["tension_lbs"] = tension_lbs

        if tension_lbs < 30 or tension_lbs > 60:
            self.add_error("tension_lbs", "張り上げテンションは 30〜60 lbs の範囲で選択してください。")

        if delivery_requested:
            cleaned_data["preferred_delivery_time"] = preferred_delivery_time
            if not delivery_location:
                self.add_error("delivery_location", "デリバリー希望の場合は、届け場所を入力してください。")
            if not preferred_delivery_time:
                self.add_error("preferred_delivery_time", "デリバリー希望の場合は、日時指定を入力してください。")
        else:
            cleaned_data["delivery_location"] = ""
            cleaned_data["preferred_delivery_time"] = (
                preferred_finish_date.strftime("%Y-%m-%d") if preferred_finish_date else ""
            )
            if not preferred_finish_date:
                self.add_error("preferred_finish_date", "デリバリーなしの場合は、希望張り上げ納期を入力してください。")

        self.instance.delivery_requested = delivery_requested
        self.instance.delivery_location = cleaned_data.get("delivery_location") or ""
        if delivery_requested:
            self.instance.preferred_delivery_time = cleaned_data.get("preferred_delivery_time") or ""
        else:
            self.instance.preferred_delivery_time = (
                preferred_finish_date.strftime("%Y-%m-%d") if preferred_finish_date else ""
            )
        self.instance.tension_lbs = tension_lbs

        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.delivery_requested = self.cleaned_data.get("delivery_requested", False)
        instance.delivery_location = self.cleaned_data.get("delivery_location") or ""
        instance.tension_lbs = int(self.cleaned_data.get("tension_lbs") or 50)
        instance.base_price = STRINGING_BASE_PRICE
        instance.delivery_fee = (
            STRINGING_DELIVERY_FEE if instance.delivery_requested else 0
        )

        if instance.delivery_requested:
            instance.preferred_delivery_time = self.cleaned_data.get("preferred_delivery_time") or ""
        else:
            preferred_finish_date = self.cleaned_data.get("preferred_finish_date")
            instance.preferred_delivery_time = preferred_finish_date.strftime("%Y-%m-%d") if preferred_finish_date else ""

        if commit:
            instance.save()
        return instance


class StringingOrderRecordForm(StringingOrderForm):
    user = forms.ModelChoiceField(label="対象顧客", queryset=User.objects.none())
    assigned_coach = forms.ModelChoiceField(
        label="ガット張り担当コーチ",
        queryset=User.objects.none(),
    )
    performed_date = forms.DateField(
        label="実績日",
        widget=forms.DateInput(attrs={"type": "date"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order_fields(
            [
                "user",
                "assigned_coach",
                "performed_date",
                "delivery_option",
                "racket_name",
                "string_name",
                "tension_lbs",
                "delivery_location",
                "preferred_delivery_time",
                "preferred_finish_date",
                "note",
            ]
        )
        self.fields["user"].queryset = User.objects.filter(
            role=User.ROLE_MEMBER,
            is_active=True,
        ).order_by("full_name", "username", "id")
        self.fields["assigned_coach"].queryset = (
            StringingOrder.supported_assigned_coaches().order_by(
                "full_name", "username", "id"
            )
        )
        self.fields["performed_date"].initial = timezone.localdate()
        self.fields["preferred_finish_date"].required = False
        self.fields["preferred_finish_date"].widget = forms.HiddenInput()

    def clean(self):
        cleaned_data = super().clean()
        if not cleaned_data.get("delivery_requested"):
            self._errors.pop("preferred_finish_date", None)
            cleaned_data["preferred_finish_date"] = cleaned_data.get("performed_date")
            if cleaned_data.get("performed_date"):
                cleaned_data["preferred_delivery_time"] = cleaned_data["performed_date"].isoformat()
        return cleaned_data


class TicketGrantAdminForm(forms.Form):
    GRANT_KIND_PAID = "paid"
    GRANT_KIND_FORMAL_FREE = "formal_free"
    GRANT_KIND_ADJUSTMENT = "adjustment"

    grant_kind = forms.ChoiceField(
        label="付与区分",
        choices=(
            (GRANT_KIND_PAID, "有料購入"),
            (GRANT_KIND_FORMAL_FREE, "無料謝礼"),
            (GRANT_KIND_ADJUSTMENT, "残高調整"),
        ),
        help_text="無料謝礼と残高調整は、メモではなくこの区分で識別します。",
    )
    idempotency_token = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    tickets = forms.IntegerField(
        label="付与枚数",
        min_value=1,
        initial=1,
        widget=forms.NumberInput(attrs={"min": 1}),
        help_text="1以上の整数で入力してください。",
    )
    unit_price = forms.IntegerField(
        label="1枚あたり金額",
        min_value=0,
        initial=4000,
        widget=forms.NumberInput(attrs={"min": 0, "step": 1}),
        help_text="円単位で入力してください。",
    )
    label = forms.CharField(
        label="表示ラベル",
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "例: 4枚セット / 管理画面付与 / キャンペーン券"}),
    )
    note = forms.CharField(
        label="メモ",
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "例: 管理画面から一括付与"}),
    )

    def clean_tickets(self):
        value = int(self.cleaned_data.get("tickets") or 0)
        if value < 1:
            raise forms.ValidationError("付与枚数は1以上にしてください。")
        return value

    def clean_unit_price(self):
        value = int(self.cleaned_data.get("unit_price") or 0)
        if value < 0:
            raise forms.ValidationError("1枚あたり金額は0以上にしてください。")
        return value

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get("grant_kind")
        unit_price = cleaned.get("unit_price")
        if unit_price is None:
            return cleaned
        if kind == self.GRANT_KIND_FORMAL_FREE and unit_price != 0:
            self.add_error("unit_price", "無料謝礼の単価は0円にしてください。")
        if kind == self.GRANT_KIND_PAID and unit_price == 0:
            self.add_error("unit_price", "有料購入の単価は1円以上にしてください。")
        return cleaned

    def clean_label(self):
        return (self.cleaned_data.get("label") or "").strip()

    def clean_note(self):
        return (self.cleaned_data.get("note") or "").strip()

    def resolved_purchase_type(self):
        grant_kind = self.cleaned_data.get("grant_kind")
        if grant_kind == self.GRANT_KIND_FORMAL_FREE:
            return TicketPurchase.PURCHASE_TYPE_FORMAL_FREE
        if grant_kind == self.GRANT_KIND_ADJUSTMENT:
            return TicketPurchase.PURCHASE_TYPE_ADMIN
        tickets = int(self.cleaned_data.get("tickets") or 0)
        unit_price = int(self.cleaned_data.get("unit_price") or 0)
        if tickets == 1 and unit_price == 4000:
            return TicketPurchase.PURCHASE_TYPE_SINGLE
        if tickets == 4 and unit_price == 3500:
            return TicketPurchase.PURCHASE_TYPE_SET4
        return TicketPurchase.PURCHASE_TYPE_ADMIN

    def resolved_reason(self):
        purchase_type = self.resolved_purchase_type()
        if purchase_type == TicketPurchase.PURCHASE_TYPE_SINGLE:
            return "purchase_single"
        if purchase_type == TicketPurchase.PURCHASE_TYPE_SET4:
            return "purchase_set4"
        return "admin_adjust"

    def resolved_label(self):
        label = (self.cleaned_data.get("label") or "").strip()
        if label:
            return label
        tickets = int(self.cleaned_data.get("tickets") or 0)
        unit_price = int(self.cleaned_data.get("unit_price") or 0)
        if tickets == 1 and unit_price == 4000:
            return "1枚券"
        if tickets == 4 and unit_price == 3500:
            return "4枚セット"
        return f"{tickets}枚 / {unit_price}円"

    def resolved_note(self):
        note = (self.cleaned_data.get("note") or "").strip()
        if note:
            return note
        return "管理画面から一括付与"


class LineAccountLinkForm(forms.ModelForm):
    class Meta:
        model = LineAccountLink
        fields = ["line_user_id", "is_active"]
        widgets = {"line_user_id": forms.TextInput(attrs={"placeholder": "LINE userId を入力"})}


ReservationForm = ReservationCreateForm
