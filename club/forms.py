from datetime import datetime, timedelta

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.utils import timezone

from .models import CoachAvailability, Court, LineAccountLink, Reservation

User = get_user_model()

BUSINESS_START_HOUR = 9
BUSINESS_END_HOUR = 21

START_HOUR_CHOICES = [(str(h), f"{h:02d}:00") for h in range(BUSINESS_START_HOUR, BUSINESS_END_HOUR)]
END_HOUR_CHOICES = [(str(h), f"{h:02d}:00") for h in range(BUSINESS_START_HOUR + 1, BUSINESS_END_HOUR + 1)]


class LoginForm(forms.Form):
    username = forms.CharField(label="ユーザー名", max_length=150)
    password = forms.CharField(label="パスワード", widget=forms.PasswordInput)


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
        full_name = (self.cleaned_data.get("full_name") or "").strip()
        email = (self.cleaned_data.get("email") or "").strip()
        phone_number = (self.cleaned_data.get("phone_number") or "").strip()

        user.full_name = full_name
        user.first_name = full_name
        user.email = email
        user.phone_number = phone_number
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
    start_date = forms.DateField(
        label="開始日",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    start_hour = forms.ChoiceField(
        label="開始時間",
        choices=START_HOUR_CHOICES,
    )
    end_date = forms.DateField(
        label="終了日",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    end_hour = forms.ChoiceField(
        label="終了時間",
        choices=END_HOUR_CHOICES,
    )

    class Meta:
        model = CoachAvailability
        fields = [
            "coach",
            "substitute_coach",
            "court",
            "lesson_type",
            "target_level",
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

        coach_queryset = User.objects.filter(role="coach").order_by("username")
        self.fields["coach"].queryset = coach_queryset
        self.fields["substitute_coach"].queryset = coach_queryset
        self.fields["substitute_coach"].required = False
        self.fields["court"].queryset = Court.objects.filter(is_active=True).order_by("name")

        self.fields["coach"].label = "担当コーチ"
        self.fields["substitute_coach"].label = "代行コーチ（その日だけ）"
        self.fields["lesson_type"].label = "レッスン種別"
        self.fields["target_level"].label = "対象レベル"
        self.fields["coach_count"].label = "担当コーチ人数"
        self.fields["court_count"].label = "利用コート面数"
        self.fields["capacity"].label = "定員"
        self.fields["custom_ticket_price"].label = "イベント用チケット価格"
        self.fields["custom_duration_hours"].label = "イベント用時間（時間）"
        self.fields["lesson_type"].initial = Reservation.LESSON_GENERAL

        self.fields["substitute_coach"].help_text = "その日のみ代行するコーチを設定できます。未設定なら通常担当のままです。"
        self.fields["coach_count"].help_text = "一般レッスンのみ使用。1人増えるごとに定員は6名、コートは1面追加されます。"
        self.fields["court_count"].help_text = "一般レッスンではコーチ人数に合わせて自動調整されます。"
        self.fields["capacity"].help_text = "一般レッスンではコーチ人数から自動計算されます。"

        if (
            self.request_user
            and not self.request_user.is_superuser
            and getattr(self.request_user, "role", "") == "coach"
        ):
            self.fields["coach"].queryset = User.objects.filter(pk=self.request_user.pk)
            self.fields["coach"].initial = self.request_user

        start_at = self.initial.get("start_at") or getattr(self.instance, "start_at", None)
        end_at = self.initial.get("end_at") or getattr(self.instance, "end_at", None)

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
        substitute_coach = cleaned_data.get("substitute_coach")

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

        expected_hours = Reservation.duration_hours_for_lesson_type(lesson_type, custom_duration_hours)
        if end_at - start_at != timedelta(hours=expected_hours):
            raise forms.ValidationError("レッスン種別に応じた時間で登録してください。")

        if coach and substitute_coach and coach.pk == substitute_coach.pk:
            cleaned_data["substitute_coach"] = None

        if lesson_type == Reservation.LESSON_GENERAL:
            if coach_count < 1:
                self.add_error("coach_count", "一般レッスンの担当コーチ人数は1以上にしてください。")
            cleaned_data["court_count"] = coach_count
            cleaned_data["capacity"] = coach_count * 6
        elif lesson_type in (Reservation.LESSON_PRIVATE, Reservation.LESSON_GROUP, Reservation.LESSON_EVENT):
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

        if commit:
            instance.save()
        return instance


class ReservationCreateForm(forms.ModelForm):
    start_date = forms.DateField(
        label="開始日",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    start_hour = forms.ChoiceField(
        label="開始時間",
        choices=START_HOUR_CHOICES,
    )
    end_date = forms.DateField(
        label="終了日",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    end_hour = forms.ChoiceField(
        label="終了時間",
        choices=END_HOUR_CHOICES,
    )

    class Meta:
        model = Reservation
        fields = [
            "coach",
            "court",
            "lesson_type",
            "requested_court_type",
            "requested_court_note",
        ]

    def __init__(self, *args, **kwargs):
        self.request_user = kwargs.pop("request_user", None)
        super().__init__(*args, **kwargs)

        self.fields["coach"].queryset = User.objects.filter(role="coach").order_by("username")
        self.fields["court"].queryset = Court.objects.filter(is_active=True).order_by("name")
        self.fields["lesson_type"].label = "レッスン種別"
        self.fields["requested_court_type"].label = "希望コート"
        self.fields["requested_court_note"].label = "コート情報"
        self.fields["lesson_type"].initial = Reservation.LESSON_GENERAL
        self.fields["requested_court_note"].widget.attrs.update(
            {"placeholder": "それ以外のコートを選んだ場合は、コート名などを入力"}
        )

        start_at = self.initial.get("start_at") or getattr(self.instance, "start_at", None)
        end_at = self.initial.get("end_at") or getattr(self.instance, "end_at", None)
        lesson_type = self.initial.get("lesson_type") or getattr(self.instance, "lesson_type", None)

        if lesson_type:
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

        expected_hours = Reservation.duration_hours_for_lesson_type(lesson_type)
        if end_at - start_at != timedelta(hours=expected_hours):
            raise forms.ValidationError("レッスン種別に応じた時間で予約してください。")

        if lesson_type == Reservation.LESSON_GROUP:
            requested_court_type = cleaned_data.get("requested_court_type")
            requested_court_note = (cleaned_data.get("requested_court_note") or "").strip()
            if requested_court_type == Court.COURT_OTHER and not requested_court_note:
                self.add_error("requested_court_note", "それ以外のコートを選択した場合は、コート情報を入力してください。")

        cleaned_data["start_at"] = start_at
        cleaned_data["end_at"] = end_at
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.start_at = self.cleaned_data["start_at"]
        instance.end_at = self.cleaned_data["end_at"]

        if commit:
            instance.save()
        return instance


class LineAccountLinkForm(forms.ModelForm):
    class Meta:
        model = LineAccountLink
        fields = ["line_user_id", "is_active"]
        widgets = {
            "line_user_id": forms.TextInput(
                attrs={"placeholder": "LINE userId を入力"}
            ),
        }


ReservationForm = ReservationCreateForm
