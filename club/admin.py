from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm

from .models import CoachAvailability, Court, LineAccountLink, Reservation, User


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


class CustomUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email", "role")


class CustomUserChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User
        fields = "__all__"


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    form = CustomUserChangeForm
    add_form = CustomUserCreationForm

    list_display = ("id", "username", "email", "role", "is_staff", "is_superuser")
    list_filter = ("role", "is_staff", "is_superuser", "is_active")
    search_fields = ("username", "email")
    ordering = ("id",)

    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("個人情報", {"fields": ("first_name", "last_name", "email")}),
        ("権限", {"fields": ("role", "is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("重要な日付", {"fields": ("last_login", "date_joined")}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("username", "email", "role", "password1", "password2", "is_staff", "is_superuser"),
            },
        ),
    )


@admin.register(Court)
class CourtAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)


@admin.register(CoachAvailability)
class CoachAvailabilityAdmin(admin.ModelAdmin):
    form = CoachAvailabilityAdminForm
    list_display = ("id", "coach", "court", "start_at", "end_at", "capacity")
    list_filter = ("coach", "court")
    search_fields = ("coach__username", "court__name")


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "coach", "court", "start_at", "end_at", "status")
    list_filter = ("status", "coach", "court")
    search_fields = ("user__username", "coach__username", "court__name")


@admin.register(LineAccountLink)
class LineAccountLinkAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "line_user_id", "is_active", "linked_at", "last_event_at")
    list_filter = ("is_active",)
    search_fields = ("user__username", "line_user_id")
