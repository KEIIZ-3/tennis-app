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
    class Meta:
        model = Reservation
        fields = ["coach", "court", "start_at", "end_at"]
        widgets = {
            "start_at": HourDateTimeInput(
                format="%Y-%m-%dT%H:%M",
                attrs={"step": 3600},
            ),
            "end_at": HourDateTimeInput(
                format="%Y-%m-%dT%H:%M",
                attrs={"step": 3600},
            ),
        }

    def __init__(self, *args, **kwargs):
        self.request_user = kwargs.pop("request_user", None)
        super().__init__(*args, **kwargs)

        self.fields["coach"].queryset = User.objects.filter(role="coach").order_by("username")
        self.fields["court"].queryset = Court.objects.filter(is_active=True).order_by("name")

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


class LineAccountLinkForm(forms.ModelForm):
    class Meta:
        model = LineAccountLink
        fields = ["line_user_id", "is_active"]
        widgets = {
            "line_user_id": forms.TextInput(
                attrs={"placeholder": "LINE userId を入力"}
            ),
        }


# views.py との互換用
ReservationForm = ReservationCreateForm
