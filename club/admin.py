\
import csv
import io
from pathlib import Path

from openpyxl import load_workbook
from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import path, reverse

from .forms import TicketGrantAdminForm
from .models import (
    CoachAvailability,
    CoachExpense,
    Court,
    FixedLesson,
    LineAccountLink,
    Reservation,
    ScheduleSurveyResponse,
    ShopEstimateRequest,
    ShopProductMaster,
    StringingOrder,
    TicketConsumption,
    TicketLedger,
    TicketPurchase,
    User,
    purchase_tickets,
)


DATETIME_INPUT_FORMATS = [
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M",
]


class AdminHourDateTimeInput(forms.DateTimeInput):
    input_type = "datetime-local"
    format = "%Y-%m-%dT%H:%M"


class ShopProductMasterImportForm(forms.Form):
    IMPORT_MODE_UPDATE = "update"
    IMPORT_MODE_REPLACE = "replace"

    IMPORT_MODE_CHOICES = (
        (IMPORT_MODE_UPDATE, "既存を残して更新 / 追加"),
        (IMPORT_MODE_REPLACE, "既存を全削除して入れ替え"),
    )

    upload_file = forms.FileField(label="取込ファイル")
    import_mode = forms.ChoiceField(
        label="取込方法",
        choices=IMPORT_MODE_CHOICES,
        initial=IMPORT_MODE_UPDATE,
    )
    default_is_active = forms.BooleanField(
        label="is_active が空欄の行は有効にする",
        required=False,
        initial=True,
    )


class CoachAvailabilityAdminForm(forms.ModelForm):
    start_at = forms.DateTimeField(
        label="Start at",
        input_formats=DATETIME_INPUT_FORMATS,
        widget=AdminHourDateTimeInput(attrs={"step": 3600}),
    )
    end_at = forms.DateTimeField(
        label="End at",
        input_formats=DATETIME_INPUT_FORMATS,
        widget=AdminHourDateTimeInput(attrs={"step": 3600}),
    )

    class Meta:
        model = CoachAvailability
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance and self.instance.pk:
            if getattr(self.instance, "start_at", None):
                self.initial["start_at"] = self.instance.start_at
            if getattr(self.instance, "end_at", None):
                self.initial["end_at"] = self.instance.end_at


class ReservationAdminForm(forms.ModelForm):
    start_at = forms.DateTimeField(
        label="Start at",
        input_formats=DATETIME_INPUT_FORMATS,
        widget=AdminHourDateTimeInput(attrs={"step": 3600}),
    )
    end_at = forms.DateTimeField(
        label="End at",
        input_formats=DATETIME_INPUT_FORMATS,
        widget=AdminHourDateTimeInput(attrs={"step": 3600}),
    )

    class Meta:
        model = Reservation
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        coach_qs = User.objects.filter(role="coach").order_by("full_name", "username", "id")
        self.fields["coach"].queryset = coach_qs
        self.fields["substitute_coach"].queryset = coach_qs
        self.fields["substitute_coach"].required = False

        if self.instance and self.instance.pk:
            if getattr(self.instance, "start_at", None):
                self.initial["start_at"] = self.instance.start_at
            if getattr(self.instance, "end_at", None):
                self.initial["end_at"] = self.instance.end_at


class FixedLessonAdminForm(forms.ModelForm):
    class Meta:
        model = FixedLesson
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["coach"].queryset = User.objects.filter(role="coach").order_by("full_name", "username", "id")


class CoachExpenseAdminForm(forms.ModelForm):
    class Meta:
        model = CoachExpense
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["created_by"].queryset = User.objects.filter(role="coach").order_by("full_name", "username", "id")
        self.fields["created_by"].required = False


class StringingOrderAdminForm(forms.ModelForm):
    class Meta:
        model = StringingOrder
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "assigned_coach" in self.fields:
            self.fields["assigned_coach"].queryset = User.objects.filter(role="coach").order_by(
                "full_name", "username", "id"
            )
            self.fields["assigned_coach"].required = False
            self.fields["assigned_coach"].label = "担当コーチ"


class CustomUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "full_name", "email", "phone_number", "role", "member_level")


class CustomUserChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User
        fields = "__all__"


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    form = CustomUserChangeForm
    add_form = CustomUserCreationForm

    list_display = (
        "id",
        "username",
        "full_name",
        "email",
        "phone_number",
        "role",
        "member_level",
        "ticket_balance",
        "is_profile_completed",
        "is_staff",
        "is_superuser",
    )
    list_filter = ("role", "member_level", "is_profile_completed", "is_staff", "is_superuser", "is_active")
    search_fields = ("username", "full_name", "email", "phone_number")
    ordering = ("id",)
    actions = ("grant_tickets_selected", "grant_single_ticket", "grant_set4_tickets")

    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("会員情報", {"fields": ("full_name", "phone_number", "email", "member_level", "ticket_balance", "is_profile_completed")}),
        ("個人情報", {"fields": ("first_name", "last_name")}),
        ("権限", {"fields": ("role", "is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("重要な日付", {"fields": ("last_login", "date_joined")}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "username",
                    "full_name",
                    "email",
                    "phone_number",
                    "role",
                    "member_level",
                    "password1",
                    "password2",
                    "is_staff",
                    "is_superuser",
                ),
            },
        ),
    )

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "grant-tickets/",
                self.admin_site.admin_view(self.grant_tickets_view),
                name="club_user_grant_tickets",
            ),
        ]
        return custom_urls + urls

    @admin.action(description="チケット付与（条件指定・複数人一括対応）")
    def grant_tickets_selected(self, request, queryset):
        selected_ids = list(queryset.values_list("pk", flat=True))
        if not selected_ids:
            self.message_user(request, "対象会員を選択してください。", level=messages.WARNING)
            return None

        ids_query = ",".join([str(pk) for pk in selected_ids])
        url = reverse("admin:club_user_grant_tickets")
        return HttpResponseRedirect(f"{url}?ids={ids_query}")

    def grant_tickets_view(self, request):
        raw_ids = (request.GET.get("ids") or request.POST.get("ids") or "").strip()
        selected_ids = [int(value) for value in raw_ids.split(",") if value.strip().isdigit()]
        queryset = User.objects.filter(pk__in=selected_ids).order_by("id")

        if not queryset.exists():
            self.message_user(request, "対象会員が見つかりません。", level=messages.WARNING)
            return redirect("admin:club_user_changelist")

        members = queryset.filter(role="member")
        skipped_users = list(queryset.exclude(role="member"))

        if request.method == "POST":
            form = TicketGrantAdminForm(request.POST)
            if form.is_valid():
                success_count = 0
                error_messages = []

                purchase_type = form.resolved_purchase_type()
                reason = form.resolved_reason()
                label = form.resolved_label()
                note = form.resolved_note()
                tickets = form.cleaned_data["tickets"]
                unit_price = form.cleaned_data["unit_price"]

                for user in members:
                    try:
                        purchase_tickets(
                            user=user,
                            tickets=tickets,
                            unit_price=unit_price,
                            purchase_type=purchase_type,
                            reason=reason,
                            note=note,
                            created_by=request.user,
                            label=label,
                        )
                        success_count += 1
                    except Exception as e:
                        error_messages.append(f"{user} の付与に失敗しました: {e}")

                if skipped_users:
                    skipped_names = "、".join([str(user) for user in skipped_users])
                    self.message_user(
                        request,
                        f"member 以外はスキップしました: {skipped_names}",
                        level=messages.WARNING,
                    )

                for message_text in error_messages:
                    self.message_user(request, message_text, level=messages.ERROR)

                if success_count:
                    self.message_user(
                        request,
                        f"{success_count}件の会員へチケットを付与しました。",
                        level=messages.SUCCESS,
                    )

                return redirect("admin:club_user_changelist")
        else:
            initial = {
                "tickets": 1,
                "unit_price": 4000,
                "label": "1枚券",
                "note": "管理画面から一括付与",
            }
            form = TicketGrantAdminForm(initial=initial)

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "チケット付与（条件指定）",
            "form": form,
            "selected_users": queryset,
            "member_users": members,
            "skipped_users": skipped_users,
            "ids": raw_ids,
        }
        return render(request, "admin/club/user/grant_tickets.html", context)

    @admin.action(description="チケット1枚を付与する（1枚 4,000円）")
    def grant_single_ticket(self, request, queryset):
        count = 0
        for user in queryset:
            if user.role != "member":
                continue
            try:
                purchase_tickets(
                    user=user,
                    tickets=1,
                    unit_price=4000,
                    purchase_type=TicketPurchase.PURCHASE_TYPE_SINGLE,
                    reason=TicketLedger.REASON_PURCHASE_SINGLE,
                    note="管理画面から1枚付与",
                    created_by=request.user,
                    label="1枚券",
                )
                count += 1
            except Exception as e:
                self.message_user(request, f"{user} の付与に失敗しました: {e}", level=messages.ERROR)
        if count:
            self.message_user(request, f"{count}件の会員へチケット1枚を付与しました。", level=messages.SUCCESS)

    @admin.action(description="4枚セットを付与する（1枚あたり 3,500円）")
    def grant_set4_tickets(self, request, queryset):
        count = 0
        for user in queryset:
            if user.role != "member":
                continue
            try:
                purchase_tickets(
                    user=user,
                    tickets=4,
                    unit_price=3500,
                    purchase_type=TicketPurchase.PURCHASE_TYPE_SET4,
                    reason=TicketLedger.REASON_PURCHASE_SET4,
                    note="管理画面から4枚セット付与",
                    created_by=request.user,
                    label="4枚セット",
                )
                count += 1
            except Exception as e:
                self.message_user(request, f"{user} の付与に失敗しました: {e}", level=messages.ERROR)
        if count:
            self.message_user(request, f"{count}件の会員へ4枚セットを付与しました。", level=messages.SUCCESS)


@admin.register(Court)
class CourtAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "court_type", "is_active")
    list_filter = ("court_type", "is_active")
    search_fields = ("name",)


@admin.register(CoachAvailability)
class CoachAvailabilityAdmin(admin.ModelAdmin):
    form = CoachAvailabilityAdminForm
    list_display = (
        "id",
        "coach",
        "court",
        "lesson_type",
        "target_level",
        "coach_count",
        "court_count",
        "capacity",
        "start_at",
        "end_at",
    )
    list_filter = ("coach", "court", "lesson_type", "target_level")
    search_fields = ("coach__username", "coach__full_name", "court__name")


@admin.register(FixedLesson)
class FixedLessonAdmin(admin.ModelAdmin):
    form = FixedLessonAdminForm
    list_display = (
        "id",
        "title",
        "coach",
        "court",
        "lesson_type",
        "target_level",
        "weekday",
        "start_hour",
        "coach_count",
        "court_count",
        "capacity",
        "weeks_ahead",
        "is_active",
    )
    list_filter = ("coach", "court", "lesson_type", "target_level", "weekday", "is_active")
    search_fields = ("title", "coach__username", "coach__full_name", "court__name", "members__username", "members__full_name")
    filter_horizontal = ("members",)
    actions = ("sync_selected_fixed_lessons",)

    @admin.action(description="選択した固定レッスンの今後予約を生成する")
    def sync_selected_fixed_lessons(self, request, queryset):
        total = 0
        for fixed_lesson in queryset:
            try:
                total += fixed_lesson.sync_future_reservations(created_by=request.user)
            except Exception as e:
                self.message_user(request, f"{fixed_lesson} の同期に失敗しました: {e}", level=messages.ERROR)
        self.message_user(request, f"固定レッスン予約を {total} 件生成しました。", level=messages.SUCCESS)


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    form = ReservationAdminForm
    list_display = (
        "id",
        "user",
        "coach",
        "substitute_coach",
        "court",
        "lesson_type",
        "target_level",
        "tickets_used",
        "start_at",
        "end_at",
        "status",
        "is_fixed_entry",
    )
    list_filter = ("status", "lesson_type", "target_level", "coach", "substitute_coach", "court", "is_fixed_entry")
    search_fields = (
        "user__username",
        "user__full_name",
        "coach__username",
        "coach__full_name",
        "substitute_coach__username",
        "substitute_coach__full_name",
        "court__name",
        "cancellation_reason",
        "requested_court_note",
        "approved_court_note",
    )


@admin.register(TicketLedger)
class TicketLedgerAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "change_amount", "balance_after", "reason", "created_by", "created_at")
    list_filter = ("reason", "created_at")
    search_fields = (
        "user__username",
        "user__full_name",
        "note",
        "reservation__court__name",
        "fixed_lesson__title",
    )
    autocomplete_fields = ("user", "reservation", "fixed_lesson", "created_by")


@admin.register(TicketPurchase)
class TicketPurchaseAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "purchase_type",
        "unit_price",
        "total_tickets",
        "remaining_tickets",
        "label",
        "purchased_at",
    )
    list_filter = ("purchase_type", "unit_price", "purchased_at")
    search_fields = ("user__username", "user__full_name", "label", "note")
    autocomplete_fields = ("user", "created_by")


@admin.register(TicketConsumption)
class TicketConsumptionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "reservation",
        "purchase",
        "tickets_used",
        "unit_price_snapshot",
        "refunded_at",
        "created_at",
    )
    list_filter = ("unit_price_snapshot", "created_at", "refunded_at")
    search_fields = (
        "user__username",
        "user__full_name",
        "reservation__court__name",
        "purchase__label",
    )
    autocomplete_fields = ("user", "purchase", "reservation", "fixed_lesson")


@admin.register(CoachExpense)
class CoachExpenseAdmin(admin.ModelAdmin):
    form = CoachExpenseAdminForm
    list_display = ("id", "expense_date", "category", "amount", "note", "created_by", "created_at")
    list_filter = ("category", "expense_date")
    search_fields = ("note",)


@admin.register(StringingOrder)
class StringingOrderAdmin(admin.ModelAdmin):
    form = StringingOrderAdminForm
    list_display = (
        "id",
        "user",
        "assigned_coach",
        "racket_name",
        "string_name",
        "tension_lbs",
        "delivery_requested",
        "base_price",
        "delivery_fee",
        "status",
        "created_at",
        "updated_at",
    )
    list_filter = ("status", "delivery_requested", "assigned_coach", "created_at")
    search_fields = (
        "user__username",
        "user__full_name",
        "assigned_coach__username",
        "assigned_coach__full_name",
        "racket_name",
        "string_name",
        "delivery_location",
        "preferred_delivery_time",
        "tension_lbs",
        "note",
    )
    autocomplete_fields = ("user",)
    readonly_fields = ("created_at", "updated_at")

    fieldsets = (
        ("依頼情報", {
            "fields": (
                "user",
                "assigned_coach",
                "status",
            )
        }),
        ("内容", {
            "fields": (
                "racket_name",
                "string_name",
                "delivery_requested",
                "tension_lbs",
                "delivery_location",
                "preferred_delivery_time",
                "note",
            )
        }),
        ("料金", {
            "fields": (
                "base_price",
                "delivery_fee",
            )
        }),
        ("日時", {
            "fields": (
                "created_at",
                "updated_at",
            )
        }),
    )


@admin.register(LineAccountLink)
class LineAccountLinkAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "line_user_id", "is_active", "linked_at", "last_event_at")
    list_filter = ("is_active",)
    search_fields = ("user__username", "user__full_name", "line_user_id")


@admin.register(ScheduleSurveyResponse)
class ScheduleSurveyResponseAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "preferred_frequency",
        "answered_at",
        "updated_at",
    )
    list_filter = ("preferred_frequency", "answered_at", "updated_at")
    search_fields = (
        "user__username",
        "user__full_name",
        "free_comment",
    )
    readonly_fields = ("answered_at", "created_at", "updated_at")


@admin.register(ShopProductMaster)
class ShopProductMasterAdmin(admin.ModelAdmin):
    change_list_template = "admin/club/shopproductmaster/change_list.html"

    list_display = (
        "id",
        "product_type",
        "category",
        "brand",
        "display_label_admin",
        "product_code",
        "official_price_display",
        "sale_price_display",
        "is_active",
        "sort_order",
        "updated_at",
    )
    list_filter = ("product_type", "category", "brand", "is_active")
    search_fields = ("product_name", "display_name", "product_code", "description")
    readonly_fields = ("created_at", "updated_at", "sale_price_display")
    list_per_page = 100
    ordering = ("brand", "category", "product_type", "sort_order", "product_name", "id")

    fieldsets = (
        ("基本情報", {
            "fields": (
                "product_type",
                "category",
                "brand",
                "product_name",
                "display_name",
                "product_code",
                "official_price",
                "sale_price_display",
                "is_active",
                "sort_order",
            )
        }),
        ("リンク", {
            "fields": (
                "product_url",
                "image_url",
            )
        }),
        ("補足", {
            "fields": (
                "description",
            )
        }),
        ("日時", {
            "fields": (
                "created_at",
                "updated_at",
            )
        }),
    )

    @admin.display(description="表示名")
    def display_label_admin(self, obj):
        return obj.display_name or obj.product_name

    @admin.display(description="定価")
    def official_price_display(self, obj):
        return f"{int(obj.official_price or 0)}円"

    @admin.display(description="販売価格")
    def sale_price_display(self, obj):
        return f"{int(obj.sale_price or 0)}円"

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "import-products/",
                self.admin_site.admin_view(self.import_products_view),
                name="club_shopproductmaster_import_products",
            ),
        ]
        return custom_urls + urls

    def import_products_view(self, request):
        if request.method == "POST":
            form = ShopProductMasterImportForm(request.POST, request.FILES)
            if form.is_valid():
                upload_file = form.cleaned_data["upload_file"]
                import_mode = form.cleaned_data["import_mode"]
                default_is_active = form.cleaned_data["default_is_active"]
                try:
                    result = self._import_uploaded_products(
                        upload_file=upload_file,
                        import_mode=import_mode,
                        default_is_active=default_is_active,
                    )
                    self.message_user(
                        request,
                        (
                            f"商品マスタ取込が完了しました。"
                            f" 追加: {result['created']}件 / 更新: {result['updated']}件 / "
                            f"スキップ: {result['skipped']}件"
                        ),
                        level=messages.SUCCESS,
                    )
                    if result["errors"]:
                        for error in result["errors"][:20]:
                            self.message_user(request, error, level=messages.WARNING)
                    return redirect("admin:club_shopproductmaster_changelist")
                except ValidationError as e:
                    for message_text in e.messages:
                        self.message_user(request, message_text, level=messages.ERROR)
                except Exception as e:
                    self.message_user(request, f"取込に失敗しました: {e}", level=messages.ERROR)
        else:
            form = ShopProductMasterImportForm()

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Shop商品マスタ一括取込",
            "form": form,
        }
        return render(request, "admin/club/shopproductmaster/import_products.html", context)

    def _import_uploaded_products(self, *, upload_file, import_mode, default_is_active):
        rows = self._read_uploaded_rows(upload_file)
        normalized_rows = self._normalize_import_rows(rows, default_is_active=default_is_active)

        created_count = 0
        updated_count = 0
        skipped_count = 0
        errors = []

        with transaction.atomic():
            if import_mode == ShopProductMasterImportForm.IMPORT_MODE_REPLACE:
                ShopProductMaster.objects.all().delete()

            for index, row in enumerate(normalized_rows, start=2):
                try:
                    instance = self._find_existing_product(row)
                    if instance is None:
                        instance = ShopProductMaster()

                    instance.product_type = row["product_type"]
                    instance.category = row["category"]
                    instance.brand = row["brand"]
                    instance.product_name = row["product_name"]
                    instance.display_name = row["display_name"]
                    instance.product_code = row["product_code"]
                    instance.official_price = row["official_price"]
                    instance.image_url = row["image_url"]
                    instance.product_url = row["product_url"]
                    instance.description = row["description"]
                    instance.sort_order = row["sort_order"]
                    instance.is_active = row["is_active"]
                    instance.full_clean()
                    is_update = bool(instance.pk)
                    instance.save()

                    if is_update:
                        updated_count += 1
                    else:
                        created_count += 1
                except ValidationError as e:
                    skipped_count += 1
                    joined = " / ".join(e.messages)
                    errors.append(f"{index}行目をスキップしました: {joined}")
                except Exception as e:
                    skipped_count += 1
                    errors.append(f"{index}行目をスキップしました: {e}")

        return {
            "created": created_count,
            "updated": updated_count,
            "skipped": skipped_count,
            "errors": errors,
        }

    def _read_uploaded_rows(self, upload_file):
        suffix = Path(upload_file.name).suffix.lower()

        if suffix == ".csv":
            decoded = upload_file.read().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(decoded))
            return list(reader)

        if suffix == ".xlsx":
            workbook = load_workbook(upload_file, data_only=True)
            sheet = self._pick_product_master_sheet(workbook)
            values = list(sheet.values)
            if not values:
                return []
            headers = [str(value).strip() if value is not None else "" for value in values[0]]
            rows = []
            for row_values in values[1:]:
                row_dict = {}
                for idx, header in enumerate(headers):
                    if not header:
                        continue
                    row_dict[header] = row_values[idx] if idx < len(row_values) else None
                rows.append(row_dict)
            return rows

        raise ValidationError("取込できるのは .csv または .xlsx ファイルのみです。")

    def _pick_product_master_sheet(self, workbook):
        preferred_names = [
            "商品マスタ_全件",
            "商品マスタ",
            "products",
            "product_master",
        ]
        expected_headers = {
            "product_type",
            "category",
            "brand",
            "product_name",
            "display_name",
            "product_code",
            "official_price",
        }

        for sheet_name in preferred_names:
            if sheet_name in workbook.sheetnames:
                sheet = workbook[sheet_name]
                headers = self._sheet_header_set(sheet)
                if expected_headers.intersection(headers):
                    return sheet

        best_sheet = None
        best_score = -1
        for sheet in workbook.worksheets:
            headers = self._sheet_header_set(sheet)
            score = len(expected_headers.intersection(headers))
            if score > best_score:
                best_score = score
                best_sheet = sheet

        if best_sheet is None or best_score <= 0:
            raise ValidationError(
                "Excel内に商品マスタの明細シートが見つかりません。"
                " 『商品マスタ_全件』シート、または product_name / brand / category などの列を含むシートを使ってください。"
            )

        return best_sheet

    def _sheet_header_set(self, sheet):
        header_values = []
        for cell_value in next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), []):
            if cell_value is None:
                continue
            header_values.append(str(cell_value).strip())
        return set(header_values)

    def _normalize_import_rows(self, rows, *, default_is_active):
        brand_map = {
            "yonex": ShopProductMaster.BRAND_YONEX,
            "YONEX": ShopProductMaster.BRAND_YONEX,
            "wilson": ShopProductMaster.BRAND_WILSON,
            "Wilson": ShopProductMaster.BRAND_WILSON,
            "babolat": ShopProductMaster.BRAND_BABOLAT,
            "Babolat": ShopProductMaster.BRAND_BABOLAT,
            "head": ShopProductMaster.BRAND_HEAD,
            "HEAD": ShopProductMaster.BRAND_HEAD,
            "prince": ShopProductMaster.BRAND_PRINCE,
            "Prince": ShopProductMaster.BRAND_PRINCE,
            "dunlop": ShopProductMaster.BRAND_DUNLOP,
            "DUNLOP": ShopProductMaster.BRAND_DUNLOP,
            "tecnifibre": ShopProductMaster.BRAND_TECHNIFIBRE,
            "Tecnifibre": ShopProductMaster.BRAND_TECHNIFIBRE,
            "other": ShopProductMaster.BRAND_OTHER,
            "その他": ShopProductMaster.BRAND_OTHER,
        }
        category_map = {
            "racket": ShopProductMaster.CATEGORY_RACKET,
            "ラケット": ShopProductMaster.CATEGORY_RACKET,
            "string": ShopProductMaster.CATEGORY_STRING,
            "ガット": ShopProductMaster.CATEGORY_STRING,
            "アクセサリ": ShopProductMaster.CATEGORY_ACCESSORY,
            "accessory": ShopProductMaster.CATEGORY_ACCESSORY,
        }
        product_type_map = {
            "main": ShopProductMaster.PRODUCT_TYPE_MAIN,
            "メイン商品": ShopProductMaster.PRODUCT_TYPE_MAIN,
            "string": ShopProductMaster.PRODUCT_TYPE_STRING,
            "ガット": ShopProductMaster.PRODUCT_TYPE_STRING,
        }

        def pick(row, *keys):
            for key in keys:
                if key in row and row[key] not in (None, ""):
                    return row[key]
            return ""

        def clean_text(value):
            return str(value or "").strip()

        def clean_int(value, default=0):
            if value in (None, ""):
                return default
            text = str(value).replace(",", "").strip()
            return int(text or default)

        def clean_bool(value, default=True):
            if value in (None, ""):
                return default
            text = str(value).strip().lower()
            if text in ("1", "true", "yes", "y", "on", "有効"):
                return True
            if text in ("0", "false", "no", "n", "off", "無効"):
                return False
            return default

        normalized = []
        for row in rows:
            product_name = clean_text(
                pick(row, "product_name", "商品名", "name", "品名", "モデル名")
            )
            if not product_name:
                continue

            brand_raw = clean_text(pick(row, "brand", "ブランド"))
            category_raw = clean_text(pick(row, "category", "カテゴリ", "category_name"))
            product_type_raw = clean_text(pick(row, "product_type", "商品種別", "type"))

            product_type = product_type_map.get(product_type_raw, ShopProductMaster.PRODUCT_TYPE_MAIN)
            category = category_map.get(category_raw, ShopProductMaster.CATEGORY_RACKET)
            brand = brand_map.get(brand_raw, ShopProductMaster.BRAND_OTHER)

            normalized.append(
                {
                    "product_type": product_type,
                    "category": ShopProductMaster.CATEGORY_STRING if product_type == ShopProductMaster.PRODUCT_TYPE_STRING else category,
                    "brand": brand,
                    "product_name": product_name,
                    "display_name": clean_text(pick(row, "display_name", "表示名")),
                    "product_code": clean_text(pick(row, "product_code", "品番", "商品コード", "code")),
                    "official_price": clean_int(pick(row, "official_price", "定価", "list_price", "price"), 0),
                    "image_url": clean_text(pick(row, "image_url", "画像URL")),
                    "product_url": clean_text(pick(row, "product_url", "商品URL", "url")),
                    "description": clean_text(pick(row, "description", "説明", "備考")),
                    "sort_order": clean_int(pick(row, "sort_order", "並び順"), 0),
                    "is_active": clean_bool(pick(row, "is_active", "公開", "active"), default_is_active),
                }
            )
        return normalized

    def _find_existing_product(self, row):
        product_code = (row.get("product_code") or "").strip()
        if product_code:
            return ShopProductMaster.objects.filter(product_code=product_code).first()

        return ShopProductMaster.objects.filter(
            brand=row["brand"],
            category=row["category"],
            product_type=row["product_type"],
            product_name=row["product_name"],
        ).first()


@admin.register(ShopEstimateRequest)
class ShopEstimateRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "created_at",
        "user_display",
        "handling_status",
        "product_category",
        "brand",
        "main_product_display",
        "main_sale_price_display",
        "string_source",
        "string_sale_price_display",
        "stringing_fee_display",
        "estimated_total_display",
    )
    list_filter = (
        "handling_status",
        "product_category",
        "brand",
        "string_source",
        "request_stringing",
        "created_at",
    )
    search_fields = (
        "user__username",
        "user__full_name",
        "main_keyword",
        "main_product_name",
        "string_keyword",
        "string_product_name",
        "note",
        "admin_note",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
        "main_sale_price_display",
        "string_sale_price_display",
        "stringing_fee_display",
        "estimated_total_display",
    )
    list_select_related = ("user",)
    list_per_page = 50
    date_hierarchy = "created_at"
    actions = (
        "mark_as_checked",
        "mark_as_ordered",
        "mark_as_completed",
        "mark_as_canceled",
    )

    fieldsets = (
        ("基本情報", {
            "fields": (
                "user",
                "handling_status",
                "product_category",
                "brand",
                "note",
                "admin_note",
            )
        }),
        ("メイン商品", {
            "fields": (
                "main_keyword",
                "main_product_name",
                "main_official_price",
                "main_sale_price_display",
            )
        }),
        ("ガット", {
            "fields": (
                "string_source",
                "string_keyword",
                "string_product_name",
                "string_official_price",
                "string_sale_price_display",
            )
        }),
        ("ガット張り", {
            "fields": (
                "request_stringing",
                "tension_lbs",
                "stringing_fee_display",
            )
        }),
        ("見積もり", {
            "fields": (
                "estimated_total_display",
            )
        }),
        ("日時", {
            "fields": (
                "created_at",
                "updated_at",
            )
        }),
    )

    @admin.display(description="会員")
    def user_display(self, obj):
        try:
            return obj.user.display_name()
        except Exception:
            return str(obj.user)

    @admin.display(description="商品")
    def main_product_display(self, obj):
        return obj.main_product_name or obj.main_keyword or "-"

    @admin.display(description="メイン販売価格")
    def main_sale_price_display(self, obj):
        return f"{obj.main_sale_price}円"

    @admin.display(description="ガット販売価格")
    def string_sale_price_display(self, obj):
        return f"{obj.string_sale_price}円"

    @admin.display(description="張り料金")
    def stringing_fee_display(self, obj):
        return f"{obj.stringing_fee}円"

    @admin.display(description="見積合計")
    def estimated_total_display(self, obj):
        return f"{obj.estimated_total}円"

    @admin.action(description="選択した物販申込を『確認済み』にする")
    def mark_as_checked(self, request, queryset):
        updated = queryset.exclude(
            handling_status=ShopEstimateRequest.HANDLING_STATUS_CHECKED
        ).update(handling_status=ShopEstimateRequest.HANDLING_STATUS_CHECKED)
        self.message_user(request, f"{updated}件を確認済みに更新しました。", level=messages.SUCCESS)

    @admin.action(description="選択した物販申込を『発注済み』にする")
    def mark_as_ordered(self, request, queryset):
        updated = queryset.exclude(
            handling_status=ShopEstimateRequest.HANDLING_STATUS_ORDERED
        ).update(handling_status=ShopEstimateRequest.HANDLING_STATUS_ORDERED)
        self.message_user(request, f"{updated}件を発注済みに更新しました。", level=messages.SUCCESS)

    @admin.action(description="選択した物販申込を『対応完了』にする")
    def mark_as_completed(self, request, queryset):
        updated = queryset.exclude(
            handling_status=ShopEstimateRequest.HANDLING_STATUS_COMPLETED
        ).update(handling_status=ShopEstimateRequest.HANDLING_STATUS_COMPLETED)
        self.message_user(request, f"{updated}件を対応完了に更新しました。", level=messages.SUCCESS)

    @admin.action(description="選択した物販申込を『キャンセル』にする")
    def mark_as_canceled(self, request, queryset):
        updated = queryset.exclude(
            handling_status=ShopEstimateRequest.HANDLING_STATUS_CANCELED
        ).update(handling_status=ShopEstimateRequest.HANDLING_STATUS_CANCELED)
        self.message_user(request, f"{updated}件をキャンセルに更新しました。", level=messages.SUCCESS)
