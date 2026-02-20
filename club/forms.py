from django import forms
from django.core.exceptions import ValidationError

from .models import (
    Reservation,
    CoachAvailability,
    User,
    TicketWallet,
)


class ReservationCreateForm(forms.ModelForm):
    class Meta:
        model = Reservation
        fields = ["kind", "tickets_used", "note", "court", "date", "start_time", "end_time", "coach"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

        if self.user is not None:
            self.instance.customer = self.user
            self.instance.status = "booked"

        self.fields["coach"].queryset = User.objects.filter(
            role="coach", is_active=True
        ).order_by("username")
        self.fields["coach"].required = True

    def clean(self):
        cleaned = super().clean()
        if self.user is None:
            return cleaned

        kind = cleaned.get("kind")
        tickets_used = cleaned.get("tickets_used") or 0

        court = cleaned.get("court")
        date = cleaned.get("date")
        start_time = cleaned.get("start_time")
        end_time = cleaned.get("end_time")
        coach = cleaned.get("coach")

        if not all([court, date, start_time, end_time, coach, kind]):
            return cleaned

        # ---- チケット系（⑤）----
        if kind in ("private_lesson", "group_lesson"):
            if tickets_used < 1:
                raise forms.ValidationError("レッスン予約は tickets_used を1以上にしてください。")

            wallet, _ = TicketWallet.objects.get_or_create(user=self.user)
            if wallet.balance < tickets_used:
                raise forms.ValidationError(f"チケット残数が足りません（残:{wallet.balance} / 必要:{tickets_used}）。")
        else:
            # court_rental
            cleaned["tickets_used"] = 0

        # ---- 1) 空き枠に収まるか（該当枠を取る）----
        slot = CoachAvailability.objects.filter(
            coach=coach,
            date=date,
            status="available",
            start_time__lte=start_time,
            end_time__gte=end_time,
        ).order_by("start_time").first()

        if not slot:
            raise forms.ValidationError("選択したコーチの空き時間外です（空き時間に収まる時間で予約してください）。")

        # ---- 2) 定員チェック（枠ごとのcapacity）----
        capacity = int(getattr(slot, "capacity", 1) or 1)
        if capacity < 1:
            capacity = 1

        # group_lesson だけ capacity を適用（privateは原則1枠）
        if kind == "group_lesson":
            booked_count = Reservation.objects.filter(
                coach=coach,
                status="booked",
                date=date,
                start_time=start_time,
                end_time=end_time,
                kind="group_lesson",
            ).count()

            if booked_count >= capacity:
                raise forms.ValidationError(f"この枠は満員です（{booked_count}/{capacity}）。")

        # ---- 3) 既存の重複チェック（コート/コーチの時間帯重複）----
        tmp = Reservation(
            customer=self.user,
            coach=coach,
            court=court,
            date=date,
            start_time=start_time,
            end_time=end_time,
            status="booked",
            kind=kind,
            tickets_used=cleaned.get("tickets_used") or 0,
            note=cleaned.get("note") or "",
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

        # court_rental はチケット0に寄せる
        if obj.kind == "court_rental":
            obj.tickets_used = 0

        if commit:
            obj.save()  # models.saveでチケット消費も走る
        return obj


class CoachAvailabilityForm(forms.ModelForm):
    class Meta:
        model = CoachAvailability
        fields = ["date", "start_time", "end_time", "capacity"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"}),
        }

    capacity = forms.IntegerField(min_value=1, initial=1, required=True)

    def __init__(self, *args, coach=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.coach = coach

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
