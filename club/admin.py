from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import (
    BusinessHours,
    CoachAvailability,
    Court,
    FacilityClosure,
    Reservation,
    TicketTransaction,
    TicketWallet,
)

User = get_user_model()


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    fieldsets = BaseUserAdmin.fieldsets + (
        ("Role", {"fields": ("role", "color")}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ("Role", {"fields": ("role", "color")}),
    )
    list_display = (
        "username",
        "email",
        "role",
        "color_preview",
        "is_active",
        "is_staff",
        "is_superuser",
    )
    list_filter = ("role", "is_active", "is_staff", "is_superuser")
    search_fields = ("username", "email", "first_name", "last_name")

    @admin.display(description="Color")
    def color_preview(self, obj):
        return obj.color


@admin.register(Court)
class CourtAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)
    ordering = ("name",)


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = (
        "date",
        "start_time",
        "end_time",
        "court",
        "coach",
        "customer",
        "kind",
        "tickets_used",
        "status",
        "can_cancel_admin",
        "updated_at",
    )
    list_filter = ("status", "kind", "court", "coach", "date")
    search_fields = (
        "customer__username",
        "customer__email",
        "coach__username",
        "coach__email",
        "court__name",
        "note",
    )
    ordering = ("-date", "-start_time", "-created_at")
    readonly_fields = ("created_at", "updated_at")
    autocomplete_fields = ("customer", "coach", "court")
    list_select_related = ("customer", "coach", "court")

    @admin.display(boolean=True, description="Can cancel now")
    def can_cancel_admin(self, obj):
        return obj.can_cancel_now


@admin.register(CoachAvailability)
class CoachAvailabilityAdmin(admin.ModelAdmin):
    list_display = (
        "date",
        "start_time",
        "end_time",
        "coach",
        "capacity",
        "remaining_count",
        "status",
        "created_at",
    )
    list_filter = ("status", "coach", "date")
    search_fields = ("coach__username", "coach__email")
    ordering = ("-date", "-start_time")
    autocomplete_fields = ("coach",)
    list_select_related = ("coach",)

    @admin.display(description="Remaining")
    def remaining_count(self, obj):
        return obj.remaining


@admin.register(BusinessHours)
class BusinessHoursAdmin(admin.ModelAdmin):
    list_display = ("weekday", "open_time", "close_time", "is_closed")
    ordering = ("weekday",)


@admin.register(FacilityClosure)
class FacilityClosureAdmin(admin.ModelAdmin):
    list_display = ("date", "reason")
    ordering = ("-date",)
    search_fields = ("reason",)


@admin.register(TicketWallet)
class TicketWalletAdmin(admin.ModelAdmin):
    list_display = ("user", "balance", "updated_at")
    search_fields = ("user__username", "user__email")
    autocomplete_fields = ("user",)
    ordering = ("user__username",)


@admin.register(TicketTransaction)
class TicketTransactionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "delta", "reason", "reservation", "note")
    list_filter = ("reason", "created_at")
    search_fields = ("user__username", "user__email", "note")
    ordering = ("-created_at",)
    autocomplete_fields = ("user", "reservation")
