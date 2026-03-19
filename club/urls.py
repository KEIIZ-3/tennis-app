from django.urls import path
from . import views

app_name = "club"

urlpatterns = [
    # 認証・基本
    path("", views.home, name="home"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("healthz/", views.healthz, name="healthz"),

    # カレンダー関連
    path("calendar/events/", views.calendar_events, name="calendar_events"),
    path("api/calendar/events/", views.calendar_events, name="calendar_events_legacy"),

    # 予約
    path("reservations/new/", views.reservation_create, name="reservation_create"),
    path("reservations/", views.reservation_list, name="reservation_list"),
    path("reservations/<int:pk>/cancel/", views.reservation_cancel, name="reservation_cancel"),

    # コーチ空き時間
    path("coach/availability/", views.coach_availability_list, name="coach_availability_list"),
    path("coach/availability/new/", views.coach_availability_create, name="coach_availability_create"),
    path("coach/availability/<int:pk>/delete/", views.coach_availability_delete, name="coach_availability_delete"),

    # LINE連携
    path("line/", views.line_link_page, name="line_link"),
    path("line/link/", views.line_link_page, name="line_link_page"),
]
