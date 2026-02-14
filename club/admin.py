from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth import get_user_model

from .models import Court, Reservation, CoachAvailability

User = get_user_model()


# -----------------------
# User
# -----------------------
@admin.register(User)
class UserAdmin(BaseUserAdmin):
    fieldsets = BaseUserAdmin.fieldsets + (
        ("Role", {"fields": ("role",)}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ("Role", {"fields": ("role",)}),
    )
    list_display = ("username", "email", "role", "is_staff", "is_superuser")
    list_filter = ("role", "is_staff", "is_superuser")


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
        "coach",     # ★追加
        "customer",
        "status",
        "created_at",
    )
    list_filter = ("date", "court", "coach", "status")
    search_fields = (
        "customer__username",
        "customer__email",
        "court__name",
        "coach__username",
        "coach__email",
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
        "status",
        "created_at",
    )
    list_filter = ("date", "coach", "status")
    search_fields = (
        "coach__username",
        "coach__email",
    )
    ordering = ("-date", "-start_time")
