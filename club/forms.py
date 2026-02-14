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

    def clean(self):
        cleaned = super().clean()
        if self.user is None:
            return cleaned

        court = cleaned.get("court")
        date = cleaned.get("date")
        start_time = cleaned.get("start_time")
        end_time = cleaned.get("end_time")
        if not all([court, date, start_time, end_time]):
            return cleaned

        tmp = Reservation(
            customer=self.user,
            court=court,
            date=date,
            start_time=start_time,
            end_time=end_time,
            status="booked",
        )

        try:
            tmp.clean()
        except ValidationError as e:
            raise forms.ValidationError(e.messages)

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

    def clean(self):
        cleaned = super().clean()
        if self.coach is None:
            return cleaned

        date = cleaned.get("date")
        start_time = cleaned.get("start_time")
        end_time = cleaned.get("end_time")
        if not all([date, start_time, end_time]):
            return cleaned

        tmp = CoachAvailability(
            coach=self.coach,
            date=date,
            start_time=start_time,
            end_time=end_time,
            status="available",
        )

        try:
            tmp.clean()
        except ValidationError as e:
            raise forms.ValidationError(e.messages)

        return cleaned

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.coach = self.coach
        obj.status = "available"
        if commit:
            obj.save()
        return obj
