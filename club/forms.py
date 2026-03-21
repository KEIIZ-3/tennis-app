from datetime import datetime

from django import forms
from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import CoachAvailability, Court, Reservation, LineAccountLink

User = get_user_model()


class LoginForm(forms.Form):
    username = forms.CharField(label="ユーザー名", max_length=150)
    password = forms.CharField(label="パスワード", widget=forms.PasswordInput)


class HourDateTimeInput(forms.DateTimeInput):
    input_type = "datetime-local"


HOUR_CHOICES = [(str(h), f"{h:02d}:00") for h in range(24)]


class CoachAvailabilityForm(forms.ModelForm):
    class Meta:
        model = CoachAvailability
        fields = ["coach", "court", "start_at", "end_at", "capacity", "note"]
        widgets = {
            "start_at": HourDateTimeInput(
                format="%Y-%m-%dT%H:%M",
                attrs={"step": 3600},
            ),
            "end_at": HourDateTimeInput(
                format="%Y-%m-%dT%H:%M",
                attrs={"step": 3600},
            ),
            "capacity": forms.NumberInput(attrs={"min": 1}),
            "note": forms.TextInput(attrs={"placeholder": "任意メモ"}),
        }

    def __init__(self, *args, **kwargs):
        self.request_user = kwargs.pop("request_user", None)
        super().__init__(*args, **kwargs)

        self.fields["coach"].queryset = User.objects.filter(role="coach").order_by("username")
        self.fields["court"].queryset = Court.objects.filter(is_active=True).order_by("name")

        if (
            self.request_user
            and not self.request_user.is_superuser
            and getattr(self.request_user, "role", "") == "coach"
        ):
            self.fields["coach"].queryset = User.objects.filter(pk=self.request_user.pk)
            self.fields["coach"].initial = self.request_user

        for field_name in ["start_at", "end_at"]:
            value = self.initial.get(field_name)
            if value and timezone.is_aware(value):
                self.initial[field_name] = timezone.localtime(value).strftime("%Y-%m-%dT%H:%M")

    def clean_start_at(self):
        value = self.cleaned_data["start_at"]
        if timezone.is_naive(value):
            value = timezone.make_aware(value)
        return value.replace(minute=0, second=0, microsecond=0)

    def clean_end_at(self):
        value = self.cleaned_data["end_at"]
        if timezone.is_naive(value):
            value = timezone.make_aware(value)
        return value.replace(minute=0, second=0, microsecond=0)


class ReservationCreateForm(forms.ModelForm):
    start_date = forms.DateField(
        label="Start at 日付",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    start_hour = forms.ChoiceField(
        label="Start at 時間",
        choices=HOUR_CHOICES,
    )
    end_date = forms.DateField(
        label="End at 日付",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    end_hour = forms.ChoiceField(
        label="End at 時間",
        choices=HOUR_CHOICES,
    )

    class Meta:
        model = Reservation
        fields = ["coach", "court"]

    def __init__(self, *args, **kwargs):
        self.request_user = kwargs.pop("request_user", None)
        super().__init__(*args, **kwargs)

        self.fields["coach"].queryset = User.objects.filter(role="coach").order_by("username")
        self.fields["court"].queryset = Court.objects.filter(is_active=True).order_by("name")

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

        if not start_date or start_hour in (None, ""):
            self.add_error("start_date", "開始日時を入力してください。")
            return cleaned_data

        if not end_date or end_hour in (None, ""):
            self.add_error("end_date", "終了日時を入力してください。")
            return cleaned_data

        start_at = self._build_aware_datetime(start_date, start_hour)
        end_at = self._build_aware_datetime(end_date, end_hour)

        if end_at <= start_at:
            raise forms.ValidationError("終了日時は開始日時より後にしてください。")

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
