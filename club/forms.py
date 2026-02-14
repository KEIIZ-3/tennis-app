# club/forms.py
from django import forms
from django.core.exceptions import ValidationError

from .models import Reservation, CoachAvailability


class ReservationCreateForm(forms.ModelForm):
    class Meta:
        model = Reservation
        fields = ["court", "date", "start_time", "end_time"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        if self.user is not None:
            # is_valid中のモデルcleanでcustomerが必要になっても落ちないようにセット
            self.instance.customer = self.user

    def clean(self):
        cleaned = super().clean()
        if self.user is None:
            return cleaned
        return cleaned

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.customer = self.user
        obj.status = "booked"
        if commit:
            obj.save()
        return obj


class CoachAvailabilityForm(forms.ModelForm):
    class Meta:
        model = CoachAvailability
        fields = ["date", "start_time", "end_time"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"}),
        }

    def __init__(self, *args, coach=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.coach = coach
        if self.coach is not None:
            # ★これが重要：is_valid中にモデルcleanが走っても落ちない
            self.instance.coach = self.coach
            self.instance.status = "available"

    def clean(self):
        cleaned = super().clean()
        if self.coach is None:
            raise forms.ValidationError("coach が未設定です（ログイン状態を確認してください）。")
        return cleaned

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.coach = self.coach
        obj.status = "available"
        if commit:
            obj.save()
        return obj

