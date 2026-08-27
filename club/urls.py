from django.urls import path
from . import views
from . import lesson_member_list
from . import coach_fixed_lesson_weekly
from . import admin_dashboard
from . import court_number_line_notice
from . import family_members
from . import settlement_views
from . import settlement_admin_refresh
from . import settlement_integrity_views
from . import lesson_execution
from . import coach_portal
from . import today_lesson_actions
from . import analytics_dashboard
from . import court_expense_transfer
from . import reservation_cancellation
from . import ticket_purchase_reservation_views
from . import shop_views

app_name = "club"

urlpatterns = [
    path("", coach_portal.home_dispatch, name="home"),
    path("login/", views.login_view, name="login"),
    path("register/", views.register_view, name="register"),
    path("profile/complete/", views.profile_complete_view, name="profile_complete"),
    path("logout/", views.logout_view, name="logout"),
    path("healthz/", views.healthz, name="healthz"),
    path("admin-dashboard/", admin_dashboard.admin_dashboard, name="admin_dashboard"),
    path("analytics/", analytics_dashboard.analytics_dashboard, name="analytics_dashboard"),
    path("lesson-calendar/", views.lesson_calendar_view, name="lesson_calendar"),
    path("lesson-calendar/confirm/", views.lesson_reservation_confirm, name="lesson_reservation_confirm"),
    path("lesson-calendar/members/", lesson_member_list.lesson_calendar_member_list, name="lesson_calendar_member_list"),
    path("calendar/events/", views.calendar_events, name="calendar_events"),
    path("api/calendar/events/", views.calendar_events, name="calendar_events_api"),
    path("tickets/", views.tickets_view, name="tickets"),
    path("tickets/purchase-reservations/", ticket_purchase_reservation_views.create, name="ticket_purchase_reservation_create"),
    path("tickets/purchase-reservations/<int:pk>/cancel/", ticket_purchase_reservation_views.cancel, name="ticket_purchase_reservation_cancel"),
    path("coach/ticket-purchases/confirm/", ticket_purchase_reservation_views.confirm, name="ticket_purchase_confirm"),
    path("coach/ticket-purchases/<int:pk>/approve/", ticket_purchase_reservation_views.approve, name="ticket_purchase_approve"),
    path("coach/ticket-purchases/<int:pk>/reverse/", ticket_purchase_reservation_views.reverse_approval, name="ticket_purchase_reverse"),
    path("family/", family_members.family_member_manage, name="family_member_manage"),
    path("help/", views.help_view, name="help"),
    path("terms/", views.terms_view, name="terms"),
    path("stringing/new/", views.stringing_order_create, name="stringing_order_create"),
    path("stringing/record/new/", views.stringing_order_record_create, name="stringing_order_record_create"),
    path("stringing/", views.stringing_order_list, name="stringing_order_list"),
    path("stringing/<int:pk>/", views.stringing_order_detail, name="stringing_order_detail"),
    path("shop/estimate/", shop_views.shop_top, name="shop_estimate"),
    path("shop/history/", shop_views.shop_history, name="shop_estimate_history"),
    path("shop/estimate/complete/<int:pk>/", views.shop_estimate_complete_view, name="shop_estimate_complete"),
    path("shop/quotes/<int:pk>/", shop_views.quote_detail, name="shop_quote_detail"),
    path("shop/quotes/<int:pk>/purchase-request/", shop_views.quote_purchase_request, name="shop_quote_purchase_request"),
    path("shop/quotes/<int:pk>/pdf/", shop_views.quote_pdf, name="shop_quote_pdf"),
    path("coach/shop/", shop_views.coach_shop, name="shop_coach"),
    path("coach/shop/quotes/new/", shop_views.quote_create, name="shop_quote_create"),
    path("coach/shop/quotes/<int:pk>/confirm/", shop_views.quote_confirm, name="shop_quote_confirm"),
    path("coach/shop/purchases/new/", shop_views.direct_purchase, name="shop_direct_purchase"),
    path("coach/shop/purchases/<int:pk>/allocation/", shop_views.allocation_edit, name="shop_allocation"),
    path("survey/", views.schedule_survey_view, name="schedule_survey"),
    path("reservations/new/", views.reservation_create, name="reservation_create"),
    path("reservations/", views.reservation_list, name="reservation_list"),
    path("reservations/<int:pk>/", views.reservation_detail, name="reservation_detail"),
    path("reservations/<int:pk>/cancel/", reservation_cancellation.reservation_cancel, name="reservation_cancel"),
    path("waitlists/<int:pk>/cancel/", views.lesson_waitlist_cancel, name="lesson_waitlist_cancel"),
    path("waitlists/<int:pk>/promote/", views.lesson_waitlist_promote, name="lesson_waitlist_promote"),
    path("coach/today-lessons/", views.coach_today_lessons, name="coach_today_lessons"),
    path("coach/lesson-quick-action/", today_lesson_actions.lesson_quick_action, name="lesson_quick_action"),
    path("coach/court-number-line/", court_number_line_notice.court_number_line_notice, name="court_number_line_notice"),
    path("coach/availability/", views.coach_availability_list, name="coach_availability_list"),
    path("coach/availability/new/", views.coach_availability_create, name="coach_availability_create"),
    path("coach/availability/<int:pk>/edit/", views.coach_availability_create, name="coach_availability_edit"),
    path("coach/availability/<int:pk>/delete/", views.coach_availability_delete, name="coach_availability_delete"),
    path("coach/requests/<int:pk>/approve/", views.coach_request_approve, name="coach_request_approve"),
    path("coach/requests/<int:pk>/reject/", views.coach_request_reject, name="coach_request_reject"),
    path("coach/fixed-lessons/", coach_fixed_lesson_weekly.coach_fixed_lesson_weekly, name="coach_fixed_lesson_weekly"),
    path("coach/ticket-summary/", views.coach_ticket_summary, name="coach_ticket_summary"),
    path("coach/payroll-summary/", settlement_views.coach_payroll_summary, name="coach_payroll_summary"),
    path("coach/revenue-summary/", views.coach_revenue_summary, name="coach_revenue_summary"),
    path("coach/admin-settlement/", settlement_admin_refresh.coach_admin_settlement, name="coach_admin_settlement"),
    path("coach/settlement-integrity/", settlement_integrity_views.settlement_integrity_diagnostic, name="settlement_integrity_diagnostic"),
    path("coach/lesson-execution/", lesson_execution.lesson_execution_manage, name="lesson_execution_manage"),
    path("coach/expenses/", court_expense_transfer.coach_expense_manage, name="coach_expense_manage"),
    path("coach/survey-summary/", views.coach_schedule_survey_summary, name="schedule_survey_summary"),
    path("coach/activity-log/", views.coach_activity_log, name="coach_activity_log"),
    path("line/", views.line_connect, name="line_connect"),
    path("line/link/", views.line_link, name="line_link"),
    path("line/webhook/", views.line_webhook, name="line_webhook"),
    path("line/login/start/", views.line_login_start, name="line_login_start"),
    path("line/login/callback/", views.line_login_callback, name="line_login_callback"),
    path("liff/", views.liff_entry, name="liff_entry"),
    path("api/liff/bootstrap/", views.liff_bootstrap, name="liff_bootstrap"),
]
