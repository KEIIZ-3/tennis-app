from django.contrib import admin
from django.db.models import Prefetch

from .models import Reservation, TicketLedger


USER_CANCELED_REASON = "会員が予約確認画面からキャンセル"


class ReservationAdminHistoryMixin:
    @admin.display(description="予約種別", ordering="is_fixed_entry")
    def reservation_kind_admin(self, obj):
        try:
            if getattr(obj, "is_fixed_entry", False) or getattr(
                obj, "fixed_lesson_id", None
            ):
                return "固定"
            return "通常"
        except Exception:
            return "-"

    @admin.display(description="キャンセル理由")
    def cancellation_reason_admin(self, obj):
        try:
            reason = (getattr(obj, "cancellation_reason", "") or "").strip()
            return reason or "-"
        except Exception:
            return "-"

    @admin.display(description="キャンセル日時", ordering="canceled_at")
    def canceled_at_admin(self, obj):
        try:
            return getattr(obj, "canceled_at", None) or "-"
        except Exception:
            return "-"

    @admin.display(description="取消元")
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

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
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
                    queryset=TicketLedger.objects.select_related(
                        "created_by"
                    ).order_by("-created_at", "-id"),
                    to_attr="_admin_ticket_ledgers",
                )
            )
        except Exception:
            return queryset
