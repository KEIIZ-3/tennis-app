from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth import get_user_model

from .models import Court, Reservation, CoachAvailability  # ← CoachAvailability追加

User = get_user_model()


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


@admin.register(Court)
class CourtAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = ("date", "start_time", "end_time", "court", "customer", "status", "created_at")
    list_filter = ("date", "court", "status")
    search_fields = ("customer__username", "customer__email", "court__name")
    ordering = ("-date", "-start_time")


@admin.register(CoachAvailability)
class CoachAvailabilityAdmin(admin.ModelAdmin):
    list_display = ("date", "start_time", "end_time", "coach", "status", "created_at")
    list_filter = ("date", "status")
    search_fields = ("coach__username", "coach__email")
    ordering = ("-date", "-start_time")
