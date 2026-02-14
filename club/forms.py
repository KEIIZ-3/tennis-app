from django import forms
from django.core.exceptions import ValidationError

from .models import Reservation, CoachAvailability, User


class ReservationCreateForm(forms.ModelForm):
    class Meta:
        model = Reservation
        fields = ["court", "date", "start_time", "end_time", "coach"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

        # customerは必ずセット（モデルcleanが走っても落ちないため）
        if self.user is not None:
            self.instance.customer = self.user
            self.instance.status = "booked"

        # coach候補は role=coach のみ
        self.fields["coach"].queryset = User.objects.filter(role="coach", is_active=True).order_by("username")
        self.fields["coach"].required = True  # コーチ紐づけを必須運用にするなら True 推奨

    def clean(self):
        cleaned = super().clean()
        if self.user is None:
            return cleaned

        court = cleaned.get("court")
        date = cleaned.get("date")
        start_time = cleaned.get("start_time")
        end_time = cleaned.get("end_time")
        coach = cleaned.get("coach")

        if not all([court, date, start_time, end_time, coach]):
            return cleaned

        # 1) コーチの空き時間に「完全に収まっている」かチェック
        ok = CoachAvailability.objects.filter(
            coach=coach,
            date=date,
            status="available",
            start_time__lte=start_time,
            end_time__gte=end_time,
        ).exists()

        if not ok:
            raise forms.ValidationError("選択したコーチの空き時間外です（空き時間に収まる時間で予約してください）。")

        # 2) Reservationモデル側の検証（コート重複・コーチ重複）も事前に通す
        tmp = Reservation(
            customer=self.user,
            coach=coach,
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

        # is_valid中に model.clean が動いても落ちないようにセット
        if self.coach is not None:
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

