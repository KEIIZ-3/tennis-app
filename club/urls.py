cat > club/urls.py <<'PY'
from django.urls import path

from . import views

app_name = "club"

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("healthz/", views.healthz, name="healthz"),

    # カレンダーイベントAPI（新）
    path("calendar/events/", views.calendar_events, name="calendar_events"),

    # カレンダーイベントAPI（旧URL互換）
    path("api/calendar/events/", views.calendar_events, name="calendar_events_api_legacy"),

    path("reservations/new/", views.reservation_create, name="reservation_create"),
    path("reservations/", views.reservation_list, name="reservation_list"),
    path("reservations/<int:pk>/cancel/", views.reservation_cancel, name="reservation_cancel"),

    path("coach/availability/", views.coach_availability_list, name="coach_availability_list"),
    path("coach/availability/new/", views.coach_availability_create, name="coach_availability_create"),
    path("coach/availability/<int:pk>/delete/", views.coach_availability_delete, name="coach_availability_delete"),

    path("line/connect/", views.line_connect_view, name="line_connect"),
    path("line/webhook/", views.line_webhook, name="line_webhook"),
]
PY
