import ast
import importlib
from datetime import datetime, time, timedelta
from pathlib import Path

from django.contrib import admin
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone

from club.admin import CoachExpenseAdmin, ReservationAdmin, UserAdmin
from club.expense_admin_type_editor import (
    EXPENSE_NOTE_META_PREFIX,
    EXPENSE_TYPE_COMMON,
    EXPENSE_TYPE_COURT_TRANSFER,
    EXPENSE_TYPE_PERSONAL,
    EditableExpenseTypeAdminForm,
    ExpenseTypeAdminMixin,
    _parse_note,
)
from club.models import CoachExpense, Court, Reservation, User
from club.reservation_admin_history import (
    ReservationAdminHistoryMixin,
    USER_CANCELED_REASON,
)
from club.user_admin_ticket_summary import UserAdminTicketSummaryMixin


ROOT = Path(__file__).resolve().parents[2]
ADMIN_FEATURE_MODULES = (
    "club.reservation_admin_history",
    "club.user_admin_ticket_summary",
    "club.expense_admin_type_editor",
)


class AdminConfigurationTests(SimpleTestCase):
    def test_registered_admins_use_explicit_mixins_and_form(self):
        self.assertIsInstance(admin.site._registry[Reservation], ReservationAdmin)
        self.assertIsInstance(admin.site._registry[User], UserAdmin)
        self.assertIsInstance(admin.site._registry[CoachExpense], CoachExpenseAdmin)
        self.assertTrue(issubclass(ReservationAdmin, ReservationAdminHistoryMixin))
        self.assertTrue(issubclass(UserAdmin, UserAdminTicketSummaryMixin))
        self.assertTrue(issubclass(CoachExpenseAdmin, ExpenseTypeAdminMixin))
        self.assertIs(CoachExpenseAdmin.form, EditableExpenseTypeAdminForm)

    def test_ready_does_not_import_admin_feature_modules(self):
        source = (ROOT / "club" / "apps.py").read_text(encoding="utf-8")
        for module_name in ADMIN_FEATURE_MODULES:
            self.assertNotIn(module_name.rsplit(".", 1)[1], source)

    def test_admin_feature_modules_have_no_registry_or_methodtype_patch(self):
        for module_name in ADMIN_FEATURE_MODULES:
            path = ROOT.joinpath(*module_name.split(".")).with_suffix(".py")
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            self.assertNotIn("admin.site._registry", source)
            self.assertNotIn("MethodType", source)
            self.assertFalse(
                any(
                    isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                    for node in tree.body
                ),
                f"{module_name} must not execute a call while importing",
            )

    def test_reimport_does_not_change_registered_admin_instances(self):
        before = {model: id(admin.site._registry[model]) for model in (Reservation, User, CoachExpense)}
        for module_name in ADMIN_FEATURE_MODULES:
            importlib.reload(importlib.import_module(module_name))
        after = {model: id(admin.site._registry[model]) for model in before}
        self.assertEqual(after, before)

    def test_reservation_columns_search_and_filters_are_preserved(self):
        expected_columns = {
            "reservation_kind_admin",
            "canceled_at_admin",
            "cancellation_source_admin",
            "cancellation_reason_admin",
        }
        self.assertTrue(expected_columns.issubset(ReservationAdmin.list_display))
        self.assertTrue({"status", "is_fixed_entry", "start_at"}.issubset(ReservationAdmin.list_filter))
        self.assertTrue(
            {
                "user__username",
                "user__full_name",
                "fixed_lesson__title",
                "cancellation_reason",
            }.issubset(ReservationAdmin.search_fields)
        )

    def test_reservation_display_values(self):
        model_admin = admin.site._registry[Reservation]
        canceled_at = timezone.now()
        reservation = Reservation(
            status=Reservation.STATUS_CANCELED,
            is_fixed_entry=True,
            canceled_at=canceled_at,
            cancellation_reason=USER_CANCELED_REASON,
        )
        self.assertEqual(model_admin.reservation_kind_admin(reservation), "固定")
        self.assertEqual(model_admin.canceled_at_admin(reservation), canceled_at)
        self.assertEqual(model_admin.cancellation_source_admin(reservation), "会員本人")
        self.assertEqual(model_admin.cancellation_reason_admin(reservation), USER_CANCELED_REASON)


class AdminBehaviorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.member = User.objects.create_user(
            username="admin-member",
            password="test",
            full_name="検索対象会員",
            ticket_balance=9,
        )
        cls.coach = User.objects.create_user(
            username="admin-coach",
            password="test",
            role=User.ROLE_COACH,
        )
        cls.court = Court.objects.create(name="管理画面テストコート")
        now = timezone.make_aware(datetime.combine(timezone.localdate(), time(12)))
        cls.past = Reservation(
            user=cls.member,
            coach=cls.coach,
            court=cls.court,
            start_at=now - timedelta(days=1),
            end_at=now - timedelta(days=1) + timedelta(hours=2),
            tickets_used=2,
            status=Reservation.STATUS_ACTIVE,
        )
        cls.future = Reservation(
            user=cls.member,
            coach=cls.coach,
            court=cls.court,
            start_at=now + timedelta(days=1),
            end_at=now + timedelta(days=1, hours=2),
            tickets_used=3,
            status=Reservation.STATUS_PENDING,
            cancellation_reason="管理検索語",
        )
        canceled = Reservation(
            user=cls.member,
            coach=cls.coach,
            court=cls.court,
            start_at=now - timedelta(days=2),
            end_at=now - timedelta(days=2) + timedelta(hours=2),
            tickets_used=7,
            status=Reservation.STATUS_CANCELED,
        )
        Reservation.objects.bulk_create([cls.past, cls.future, canceled])

    def test_user_ticket_summary_uses_existing_formula(self):
        request = RequestFactory().get("/admin/club/user/")
        model_admin = admin.site._registry[User]
        member = model_admin.get_queryset(request).get(pk=self.member.pk)
        self.assertEqual(model_admin.consumed_tickets_admin(member), 2)
        self.assertEqual(model_admin.planned_tickets_admin(member), 3)
        self.assertEqual(model_admin.current_tickets_admin(member), 9)

    def test_reservation_admin_search_works(self):
        request = RequestFactory().get("/admin/club/reservation/", {"q": "管理検索語"})
        model_admin = admin.site._registry[Reservation]
        queryset, _ = model_admin.get_search_results(
            request,
            model_admin.get_queryset(request),
            "管理検索語",
        )
        self.assertEqual(list(queryset.values_list("pk", flat=True)), [self.future.pk])

    def test_reservation_admin_list_preserves_canceled_reservations(self):
        request = RequestFactory().get("/admin/club/reservation/")
        model_admin = admin.site._registry[Reservation]

        statuses = list(
            model_admin.get_queryset(request).order_by("pk").values_list("status", flat=True)
        )

        self.assertIn(Reservation.STATUS_ACTIVE, statuses)
        self.assertIn(Reservation.STATUS_PENDING, statuses)
        self.assertIn(Reservation.STATUS_CANCELED, statuses)

    def test_expense_form_reads_edits_and_saves_legacy_note(self):
        expense = CoachExpense(note="既存メモ")
        form = EditableExpenseTypeAdminForm(instance=expense)
        self.assertEqual(form.initial["note"], "既存メモ")
        self.assertEqual(form.initial["expense_type"], EXPENSE_TYPE_COMMON)

        bound = EditableExpenseTypeAdminForm(
            data={
                "expense_date": timezone.localdate().isoformat(),
                "category": CoachExpense.CATEGORY_OTHER,
                "amount": "1200",
                "note": "更新メモ",
                "expense_type": EXPENSE_TYPE_PERSONAL,
                "created_by": "",
                "settlement_period_start": "",
                "settlement_period_end": "",
            },
            instance=expense,
        )
        self.assertTrue(bound.is_valid(), bound.errors)
        saved = bound.save(commit=False)
        parsed = _parse_note(saved.note)
        self.assertEqual(parsed["expense_type"], EXPENSE_TYPE_PERSONAL)
        self.assertEqual(parsed["plain_note"], "更新メモ")
        self.assertTrue(saved.note.startswith(EXPENSE_NOTE_META_PREFIX))

    def test_court_transfer_type_remains_read_only(self):
        expense = CoachExpense(
            note=(
                f'{EXPENSE_NOTE_META_PREFIX}'
                '{"expense_type":"court_transfer"}\nコート代'
            )
        )
        form = EditableExpenseTypeAdminForm(instance=expense)
        self.assertTrue(form.fields["expense_type"].disabled)
        self.assertEqual(form.fields["expense_type"].choices[0][0], EXPENSE_TYPE_COURT_TRANSFER)
        self.assertEqual(_parse_note(expense.note)["plain_note"], "コート代")
