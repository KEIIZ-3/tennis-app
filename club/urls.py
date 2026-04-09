from django.urls import path
from . import views

app_name = "club"

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.login_view, name="login"),
    path("register/", views.register_view, name="register"),
    path("profile/complete/", views.profile_complete_view, name="profile_complete"),
    path("logout/", views.logout_view, name="logout"),

    path("healthz/", views.healthz, name="healthz"),

    path("lesson-calendar/", views.lesson_calendar_view, name="lesson_calendar"),

    path("calendar/events/", views.calendar_events, name="calendar_events"),
    path("api/calendar/events/", views.calendar_events, name="calendar_events_api"),

    path("tickets/", views.tickets_view, name="tickets"),
    path("help/", views.help_view, name="help"),
    path("terms/", views.terms_view, name="terms"),
    path("stringing/new/", views.stringing_order_create, name="stringing_order_create"),
    path("stringing/", views.stringing_order_list, name="stringing_order_list"),
    path("shop/estimate/", views.shop_estimate_view, name="shop_estimate"),
    path("shop/history/", views.shop_estimate_history_view, name="shop_estimate_history"),
    path("shop/estimate/complete/<int:pk>/", views.shop_estimate_complete_view, name="shop_estimate_complete"),

    path("survey/", views.schedule_survey_view, name="schedule_survey"),

    path("reservations/new/", views.reservation_create, name="reservation_create"),
    path("reservations/", views.reservation_list, name="reservation_list"),
    path("reservations/<int:pk>/cancel/", views.reservation_cancel, name="reservation_cancel"),

    path("coach/availability/", views.coach_availability_list, name="coach_availability_list"),
    path("coach/availability/new/", views.coach_availability_create, name="coach_availability_create"),
    path("coach/availability/<int:pk>/edit/", views.coach_availability_create, name="coach_availability_edit"),
    path("coach/availability/<int:pk>/delete/", views.coach_availability_delete, name="coach_availability_delete"),

    path("coach/requests/<int:pk>/approve/", views.coach_request_approve, name="coach_request_approve"),
    path("coach/requests/<int:pk>/reject/", views.coach_request_reject, name="coach_request_reject"),

    path("coach/fixed-lessons/", views.coach_fixed_lesson_weekly, name="coach_fixed_lesson_weekly"),
    path("coach/ticket-summary/", views.coach_ticket_summary, name="coach_ticket_summary"),
    path("coach/payroll-summary/", views.coach_payroll_summary, name="coach_payroll_summary"),
    path("coach/expenses/", views.coach_expense_manage, name="coach_expense_manage"),
    path("coach/survey-summary/", views.coach_schedule_survey_summary, name="schedule_survey_summary"),

    path("line/", views.line_connect, name="line_connect"),
    path("line/link/", views.line_link, name="line_link"),
    path("line/webhook/", views.line_webhook, name="line_webhook"),

    path("line/login/start/", views.line_login_start, name="line_login_start"),
    path("line/login/callback/", views.line_login_callback, name="line_login_callback"),

    path("liff/", views.liff_entry, name="liff_entry"),
    path("api/liff/bootstrap/", views.liff_bootstrap, name="liff_bootstrap"),
]
