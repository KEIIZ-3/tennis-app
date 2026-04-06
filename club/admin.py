from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
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


@admin.register(ShopEstimateRequest)
class ShopEstimateRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "created_at",
        "user_display",
        "product_category",
        "brand",
        "main_product_display",
        "main_sale_price_display",
        "string_source",
        "string_sale_price_display",
        "stringing_fee_display",
        "estimated_total_display",
    )
    list_filter = ("product_category", "brand", "string_source", "request_stringing", "created_at")
    search_fields = (
        "user__username",
        "user__full_name",
        "main_keyword",
        "main_product_name",
        "string_keyword",
        "string_product_name",
        "note",
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

    fieldsets = (
        ("基本情報", {
            "fields": (
                "user",
                "product_category",
                "brand",
                "note",
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
