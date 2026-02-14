# club/urls.py
from django.urls import path
from . import views

app_name = "club"

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),

    # 予約
    path("reservations/", views.reservation_list, name="reservation_list"),
    path("reservations/new/", views.reservation_create, name="reservation_create"),
    path("reservations/<int:pk>/cancel/", views.reservation_cancel, name="reservation_cancel"),

    # コーチ空き
    path("coach/availability/", views.coach_availability_list, name="coach_availability_list"),
    path("coach/availability/new/", views.coach_availability_create, name="coach_availability_create"),
    path("coach/availability/<int:pk>/delete/", views.coach_availability_delete, name="coach_availability_delete"),
]
