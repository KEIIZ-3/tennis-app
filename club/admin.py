from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth import get_user_model

from .models import (
    Court,
    Reservation,
    CoachAvailability,
    BusinessHours,
    FacilityClosure,
    TicketWallet,
    TicketTransaction,
)

User = get_user_model()


# -----------------------
# User
# -----------------------
@admin.register(User)
class UserAdmin(BaseUserAdmin):
    fieldsets = BaseUserAdmin.fieldsets + (
        ("Role", {"fields": ("role", "color")}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ("Role", {"fields": ("role", "color")}),
    )
    list_display = ("username", "email", "role", "color", "is_staff", "is_superuser")
    list_filter = ("role", "is_staff", "is_superuser")
    search_fields = ("username", "email")


# -----------------------
# Court
# -----------------------
@admin.register(Court)
class CourtAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)


# -----------------------
# Reservation
# -----------------------
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
        "updated_at",
        "created_at",
    )
    list_filter = ("date", "court", "coach", "kind", "status")
    search_fields = (
        "customer__username",
        "customer__email",
        "court__name",
        "coach__username",
        "coach__email",
        "note",
    )
    ordering = ("-date", "-start_time")


# -----------------------
# CoachAvailability
# -----------------------
@admin.register(CoachAvailability)
class CoachAvailabilityAdmin(admin.ModelAdmin):
    list_display = (
        "date",
        "start_time",
        "end_time",
        "coach",
        "capacity",
        "status",
        "created_at",
    )
    list_filter = ("date", "coach", "status")
    search_fields = (
        "coach__username",
        "coach__email",
    )
    ordering = ("-date", "-start_time")


# -----------------------
# BusinessHours / FacilityClosure
# -----------------------
@admin.register(BusinessHours)
class BusinessHoursAdmin(admin.ModelAdmin):
    list_display = ("weekday", "open_time", "close_time", "is_closed")
    ordering = ("weekday",)


@admin.register(FacilityClosure)
class FacilityClosureAdmin(admin.ModelAdmin):
    list_display = ("date", "reason")
    ordering = ("-date",)


# -----------------------
# Tickets
# -----------------------
@admin.register(TicketWallet)
class TicketWalletAdmin(admin.ModelAdmin):
    list_display = ("user", "balance", "updated_at")
    search_fields = ("user__username", "user__email")


@admin.register(TicketTransaction)
class TicketTransactionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "delta", "reason", "reservation", "note")
    list_filter = ("reason",)
    search_fields = ("user__username", "user__email", "note")
    ordering = ("-created_at",)
