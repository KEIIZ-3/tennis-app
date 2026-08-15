from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.template.loader import get_template
from django.test import TestCase
from django.urls import reverse


class OperationsResponsiveTemplateTests(TestCase):
    template_names = (
        "coach/admin_settlement.html",
        "coach/payroll_summary.html",
        "coach/today_lessons.html",
        "coach/lesson_execution_manage.html",
        "coach/settlement_integrity_diagnostic.html",
        "coach/admin_dashboard.html",
    )

    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user(
            username="responsive_staff",
            password="password12345",
            role=User.ROLE_COACH,
            is_staff=True,
        )

    def test_related_templates_compile_and_keep_existing_url_names(self):
        for template_name in self.template_names:
            with self.subTest(template=template_name):
                get_template(template_name)

        expected_names = (
            "coach_admin_settlement",
            "coach_payroll_summary",
            "coach_today_lessons",
            "lesson_execution_manage",
            "settlement_integrity_diagnostic",
            "admin_dashboard",
        )
        for name in expected_names:
            with self.subTest(url_name=name):
                self.assertTrue(reverse(f"club:{name}"))

    def test_mobile_contract_classes_cover_state_amount_and_action_order(self):
        settlement_source = get_template(
            "coach/admin_settlement.html"
        ).template.source
        execution_source = get_template(
            "coach/lesson_execution_manage.html"
        ).template.source
        today_source = get_template("coach/today_lessons.html").template.source

        self.assertIn('class="panel priority-alert" role="alert"', settlement_source)
        self.assertIn("operation-record", settlement_source)
        self.assertLess(
            settlement_source.index(">未確定<"),
            settlement_source.index("record-amount")
            if "record-amount" in settlement_source
            else settlement_source.index("record-action"),
        )
        self.assertIn("fact-status", execution_source)
        self.assertIn("fact-court", execution_source)
        self.assertIn("court_payer_name", execution_source)
        self.assertIn("status-{{ row.execution_status", today_source)

    @patch("club.settlement_integrity_views.affected_closed_settlements")
    def test_diagnostic_renders_mobile_cards_for_difference_states(
        self, diagnostics
    ):
        diagnostics.return_value = [
            {
                "month_label": "2026年7月",
                "scheduled_count": 2,
                "ball_amount": 1500000,
                "occurrences": [
                    {"label": "長いレッスン名" * 8, "url": "/execution/"}
                ],
                "coach_rows": [
                    {
                        "coach_name": "非常に長いコーチ氏名" * 4,
                        "old_count": 3,
                        "current_count": 2,
                        "saved_burden_registered": True,
                        "saved_burden": 0,
                        "reference_burden": 1200000,
                        "difference": 1200000,
                    },
                    {
                        "coach_name": "差額なしコーチ",
                        "old_count": 1,
                        "current_count": 1,
                        "saved_burden_registered": True,
                        "saved_burden": -500,
                        "reference_burden": -500,
                        "difference": 0,
                    },
                ],
            }
        ]
        self.client.force_login(self.staff)

        response = self.client.get(reverse("club:settlement_integrity_diagnostic"))

        self.assertContains(response, "diagnostic-warning")
        self.assertContains(response, "mobile-card-list diagnostic-coach-list")
        self.assertContains(response, "差額あり")
        self.assertContains(response, "差額なし")
        self.assertContains(response, "1200000円")
        self.assertContains(response, "-500円")

    def test_common_responsive_stylesheet_is_loaded(self):
        source = get_template("base.html").template.source
        self.assertIn("club/operations-responsive.css", source)
        self.assertIn('class="operations-shell"', source)

    def test_operations_shell_matches_staff_superuser_and_role_access(self):
        User = get_user_model()
        cases = (
            ("staff_member", User.ROLE_MEMBER, True, False, True),
            ("superuser_member", User.ROLE_MEMBER, False, True, True),
            ("coach_user", User.ROLE_COACH, False, False, True),
            ("ordinary_member", User.ROLE_MEMBER, False, False, False),
        )
        for username, role, is_staff, is_superuser, expected in cases:
            with self.subTest(username=username):
                user = User.objects.create_user(
                    username=username,
                    password="password12345",
                    role=role,
                    is_staff=is_staff,
                    is_superuser=is_superuser,
                    is_profile_completed=True,
                )
                self.client.force_login(user)
                url_name = "home" if expected else "terms"
                response = self.client.get(reverse(f"club:{url_name}"))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    'class="operations-shell"' in response.content.decode(),
                    expected,
                )

    def test_admin_dashboard_destinations_use_responsive_templates(self):
        dashboard_source = get_template("coach/admin_dashboard.html").template.source
        destination_templates = {
            "analytics_dashboard": "coach/analytics_dashboard.html",
            "coach_today_lessons": "coach/today_lessons.html",
            "lesson_calendar": "lesson_calendar.html",
            "reservation_list": "reservations/list.html",
            "coach_expense_manage": "coach/court_expense_transfer.html",
            "coach_revenue_summary": "coach/revenue_summary.html",
            "coach_admin_settlement": "coach/admin_settlement.html",
            "stringing_order_list": "stringing/list.html",
            "coach_fixed_lesson_weekly": "coach/fixed_lesson_weekly.html",
            "coach_availability_list": "coach/availability_list.html",
            "lesson_execution_manage": "coach/lesson_execution_manage.html",
            "coach_ticket_summary": "coach/ticket_summary.html",
        }
        for url_name, template_name in destination_templates.items():
            with self.subTest(url_name=url_name):
                self.assertIn("'club:%s'" % url_name, dashboard_source)
                source = get_template(template_name).template.source
                self.assertIn('{% extends "base.html" %}', source)

    def test_management_tables_keep_internal_scroll_or_mobile_cards(self):
        contracts = {
            "coach/revenue_summary.html": "revenue-table-wrap",
            "coach/ticket_summary.html": "mobile-card-area",
            "coach/availability_list.html": "mobile-schedule-cards",
            "reservations/list.html": "reservation-item",
        }
        for template_name, responsive_hook in contracts.items():
            with self.subTest(template=template_name):
                source = get_template(template_name).template.source
                self.assertIn(responsive_hook, source)

    def test_settlement_keeps_people_join_separate_from_name_wrapping(self):
        source = get_template("coach/admin_settlement.html").template.source
        self.assertIn('coach_names|join:" / "', source)
        self.assertIn('class="person-name"', source)
        self.assertIn('class="record-date date-time"', source)

    def test_common_mobile_foundation_covers_forms_buttons_and_tables(self):
        with open("club/static/club/operations-responsive.css", encoding="utf-8") as css_file:
            source = css_file.read()
        self.assertIn(".operations-shell .main select", source)
        self.assertIn("min-height:44px", source)
        self.assertIn("overflow-x:auto", source)
        self.assertIn("@media(max-width:430px)", source)
        self.assertIn(".revenue-table-wrap", source)
        self.assertNotIn("word-break:break-all", source.replace(" ", ""))
