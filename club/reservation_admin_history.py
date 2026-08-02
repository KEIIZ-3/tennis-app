from types import MethodType

from django.contrib import admin
from django.db.models import Prefetch

from .models import Reservation, TicketLedger


USER_CANCELED_REASON = "会員が予約確認画面からキャンセル"


def reservation_kind_admin(self, obj):
    try:
        if getattr(obj, "is_fixed_entry", False) or getattr(obj, "fixed_lesson_id", None):
            return "固定"
        return "通常"
    except Exception:
        return "-"


reservation_kind_admin.short_description = "予約種別"
reservation_kind_admin.admin_order_field = "is_fixed_entry"


def cancellation_reason_admin(self, obj):
    try:
        reason = (getattr(obj, "cancellation_reason", "") or "").strip()
        return reason or "-"
    except Exception:
        return "-"


cancellation_reason_admin.short_description = "キャンセル理由"


def canceled_at_admin(self, obj):
    try:
        return getattr(obj, "canceled_at", None) or "-"
    except Exception:
        return "-"


canceled_at_admin.short_description = "キャンセル日時"
canceled_at_admin.admin_order_field = "canceled_at"


def cancellation_source_admin(self, obj):
    try:
        if getattr(obj, "status", "") == getattr(
            Reservation,
            "STATUS_RAIN_CANCELED",
            "rain_canceled",
        ):
            return "雨天中止"

        reason = (getattr(obj, "cancellation_reason", "") or "").strip()
        if not reason:
            return "-"

        if reason == USER_CANCELED_REASON:
            return "会員本人"
        if "固定レッスンメンバー解除" in reason:
            return "固定メンバー変更"
        if "開催回数変更" in reason:
            return "固定レッスン同期"

        ledgers = getattr(obj, "_admin_ticket_ledgers", None)
        if ledgers is None:
            try:
                ledgers = list(
                    TicketLedger.objects.filter(reservation=obj)
                    .select_related("created_by")
                    .order_by("-created_at", "-id")
                )
            except Exception:
                ledgers = []

        for ledger in ledgers:
            try:
                note = (getattr(ledger, "note", "") or "").strip()
                if reason and reason not in note:
                    continue
                actor = getattr(ledger, "created_by", None)
                if actor:
                    try:
                        return actor.display_name()
                    except Exception:
                        return str(actor)
            except Exception:
                continue

        return "管理・システム処理"
    except Exception:
        return "-"


cancellation_source_admin.short_description = "取消元"


def apply_reservation_admin_history_columns():
    model_admin = admin.site._registry.get(Reservation)
    if model_admin is None:
        return

    model_admin.reservation_kind_admin = MethodType(
        reservation_kind_admin,
        model_admin,
    )
    model_admin.cancellation_reason_admin = MethodType(
        cancellation_reason_admin,
        model_admin,
    )
    model_admin.canceled_at_admin = MethodType(
        canceled_at_admin,
        model_admin,
    )
    model_admin.cancellation_source_admin = MethodType(
        cancellation_source_admin,
        model_admin,
    )

    current_display = list(model_admin.list_display)
    replacement_display = []
    for field_name in current_display:
        replacement_display.append(field_name)
        if field_name == "status":
            replacement_display.extend(
                [
                    "reservation_kind_admin",
                    "canceled_at_admin",
                    "cancellation_source_admin",
                    "cancellation_reason_admin",
                ]
            )
    model_admin.list_display = tuple(dict.fromkeys(replacement_display))

    current_filters = list(model_admin.list_filter)
    for field_name in ("status", "is_fixed_entry", "start_at"):
        if field_name not in current_filters:
            current_filters.append(field_name)
    model_admin.list_filter = tuple(current_filters)

    current_search = list(model_admin.search_fields)
    for field_name in (
        "user__username",
        "user__full_name",
        "fixed_lesson__title",
        "cancellation_reason",
    ):
        if field_name not in current_search:
            current_search.append(field_name)
    model_admin.search_fields = tuple(current_search)

    original_get_queryset = model_admin.get_queryset

    def get_queryset(self, request):
        queryset = original_get_queryset(request)
        try:
            return queryset.select_related(
                "user",
                "coach",
                "substitute_coach",
                "court",
                "fixed_lesson",
            ).prefetch_related(
                Prefetch(
                    "ticket_ledgers",
                    queryset=TicketLedger.objects.select_related("created_by").order_by(
                        "-created_at",
                        "-id",
                    ),
                    to_attr="_admin_ticket_ledgers",
                )
            )
        except Exception:
            return queryset

    model_admin.get_queryset = MethodType(get_queryset, model_admin)


apply_reservation_admin_history_columns()
