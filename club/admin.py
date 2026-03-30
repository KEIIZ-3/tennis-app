from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm

from .models import (
    CoachAvailability,
    Court,
    FixedLesson,
    LineAccountLink,
    Reservation,
    TicketLedger,
    User,
    apply_ticket_change,
)
from .notifications import (
    build_reservation_rain_canceled_message,
    notify_user,
)


class AdminHourDateTimeInput(forms.DateTimeInput):
    input_type = "datetime-local"


class CoachAvailabilityAdminForm(forms.ModelForm):
    class Meta:
        model = CoachAvailability
        fields = "__all__"
        widgets = {
            "start_at": AdminHourDateTimeInput(
                format="%Y-%m-%dT%H:%M",
                attrs={"step": 3600},
            ),
            "end_at": AdminHourDateTimeInput(
                format="%Y-%m-%dT%H:%M",
                attrs={"step": 3600},
            ),
        }


class ReservationAdminForm(forms.ModelForm):
    class Meta:
        model = Reservation
        fields = "__all__"
        widgets = {
            "start_at": AdminHourDateTimeInput(
                format="%Y-%m-%dT%H:%M",
                attrs={"step": 3600},
            ),
            "end_at": AdminHourDateTimeInput(
                format="%Y-%m-%dT%H:%M",
                attrs={"step": 3600},
            ),
        }


class CustomUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "full_name", "email", "phone_number", "role", "member_level")


class CustomUserChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User
        fields = "__all__"


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    form = CustomUserChangeForm
    add_form = CustomUserCreationForm

    list_display = (
        "id",
        "username",
        "full_name",
        "email",
        "phone_number",
        "role",
        "member_level",
        "ticket_balance",
        "is_profile_completed",
        "is_staff",
        "is_superuser",
    )
    list_filter = ("role", "member_level", "is_profile_completed", "is_staff", "is_superuser", "is_active")
    search_fields = ("username", "full_name", "email", "phone_number")
    ordering = ("id",)
    actions = ("grant_single_ticket", "grant_set4_tickets")

    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("会員情報", {"fields": ("full_name", "phone_number", "email", "member_level", "ticket_balance", "is_profile_completed")}),
        ("個人情報", {"fields": ("first_name", "last_name")}),
        ("権限", {"fields": ("role", "is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("重要な日付", {"fields": ("last_login", "date_joined")}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "username",
                    "full_name",
                    "email",
                    "phone_number",
                    "role",
                    "member_level",
                    "password1",
                    "password2",
                    "is_staff",
                    "is_superuser",
                ),
            },
        ),
    )

    @admin.action(description="チケット1枚を付与する（現金受取など）")
    def grant_single_ticket(self, request, queryset):
        count = 0
        for user in queryset:
            try:
                apply_ticket_change(
                    user=user,
                    amount=1,
                    reason=TicketLedger.REASON_PURCHASE_SINGLE,
                    note="管理画面から1枚付与",
                    created_by=request.user,
                )
                count += 1
            except Exception as e:
                self.message_user(request, f"{user} の付与に失敗しました: {e}", level=messages.ERROR)
        if count:
            self.message_user(request, f"{count}件の会員へチケット1枚を付与しました。", level=messages.SUCCESS)

    @admin.action(description="4枚セットを付与する（現金受取など）")
    def grant_set4_tickets(self, request, queryset):
        count = 0
        for user in queryset:
            try:
                apply_ticket_change(
                    user=user,
                    amount=4,
                    reason=TicketLedger.REASON_PURCHASE_SET4,
                    note="管理画面から4枚セット付与",
                    created_by=request.user,
                )
                count += 1
            except Exception as e:
                self.message_user(request, f"{user} の付与に失敗しました: {e}", level=messages.ERROR)
        if count:
            self.message_user(request, f"{count}件の会員へ4枚セットを付与しました。", level=messages.SUCCESS)


@admin.register(Court)
class CourtAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "court_type", "is_active")
    list_filter = ("court_type", "is_active")
    search_fields = ("name",)


@admin.register(CoachAvailability)
class CoachAvailabilityAdmin(admin.ModelAdmin):
    form = CoachAvailabilityAdminForm
    list_display = ("id", "coach", "court", "lesson_type", "target_level", "start_at", "end_at", "capacity")
    list_filter = ("coach", "court", "lesson_type", "target_level")
    search_fields = ("coach__username", "coach__full_name", "court__name")
    actions = ("rain_cancel_selected_slots",)

    @admin.action(description="選択した枠を雨天中止にして参加者へLINE送信する")
    def rain_cancel_selected_slots(self, request, queryset):
        canceled_count = 0

        for availability in queryset:
            reservations = availability.reservations.filter(status=Reservation.STATUS_ACTIVE).select_related("user", "coach", "court")
            for reservation in reservations:
                try:
                    changed = reservation.mark_rain_canceled(
                        created_by=request.user,
                        reason="雨天中止",
                    )
                    if changed:
                        notify_user(reservation.user, build_reservation_rain_canceled_message(reservation))
                        canceled_count += 1
                except Exception as e:
                    self.message_user(
                        request,
                        f"{reservation} の雨天中止処理に失敗しました: {e}",
                        level=messages.ERROR,
                    )

        if canceled_count:
            self.message_user(
                request,
                f"{canceled_count}件の予約を雨天中止にし、参加者へLINE送信しました。",
                level=messages.SUCCESS,
            )
        else:
            self.message_user(request, "対象となる予約はありませんでした。", level=messages.INFO)


@admin.register(FixedLesson)
class FixedLessonAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "coach", "court", "lesson_type", "target_level", "weekday", "start_hour", "capacity", "weeks_ahead", "is_active")
    list_filter = ("coach", "court", "lesson_type", "target_level", "weekday", "is_active")
    search_fields = ("title", "coach__username", "coach__full_name", "court__name", "members__username", "members__full_name")
    filter_horizontal = ("members",)
    actions = ("sync_selected_fixed_lessons",)

    @admin.action(description="選択した固定レッスンの今後予約を生成する")
    def sync_selected_fixed_lessons(self, request, queryset):
        total = 0
        for fixed_lesson in queryset:
            try:
                total += fixed_lesson.sync_future_reservations(created_by=request.user)
            except Exception as e:
                self.message_user(request, f"{fixed_lesson} の同期に失敗しました: {e}", level=messages.ERROR)
        self.message_user(request, f"固定レッスン予約を {total} 件生成しました。", level=messages.SUCCESS)


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    form = ReservationAdminForm
    list_display = (
        "id",
        "user",
        "coach",
        "court",
        "lesson_type",
        "target_level",
        "tickets_used",
        "start_at",
        "end_at",
        "status",
        "is_fixed_entry",
    )
    list_filter = ("status", "lesson_type", "target_level", "coach", "court", "is_fixed_entry")
    search_fields = (
        "user__username",
        "user__full_name",
        "coach__username",
        "coach__full_name",
        "court__name",
        "cancellation_reason",
        "requested_court_note",
        "approved_court_note",
    )

    def get_actions(self, request):
        actions = super().get_actions(request)
        if "rain_cancel_selected_reservations" in actions:
            del actions["rain_cancel_selected_reservations"]
        return actions


@admin.register(TicketLedger)
class TicketLedgerAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "change_amount", "balance_after", "reason", "created_by", "created_at")
    list_filter = ("reason", "created_at")
    search_fields = (
        "user__username",
        "user__full_name",
        "note",
        "reservation__court__name",
        "fixed_lesson__title",
    )
    autocomplete_fields = ("user", "reservation", "fixed_lesson", "created_by")


@admin.register(LineAccountLink)
class LineAccountLinkAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "line_user_id", "is_active", "linked_at", "last_event_at")
    list_filter = ("is_active",)
    search_fields = ("user__username", "user__full_name", "line_user_id")
