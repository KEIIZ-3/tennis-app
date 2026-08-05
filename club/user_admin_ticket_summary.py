from django.db.models import IntegerField, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import Reservation


VALID_RESERVATION_STATUSES = (
    Reservation.STATUS_ACTIVE,
    Reservation.STATUS_PENDING,
)


class UserAdminTicketSummaryMixin:
    def consumed_tickets_admin(self, obj):
        try:
            return int(getattr(obj, "_consumed_ticket_count", 0) or 0)
        except Exception:
            return 0

    consumed_tickets_admin.short_description = "消費済みチケット"
    consumed_tickets_admin.admin_order_field = "_consumed_ticket_count"

    def planned_tickets_admin(self, obj):
        try:
            return int(getattr(obj, "_planned_ticket_count", 0) or 0)
        except Exception:
            return 0

    planned_tickets_admin.short_description = "消費予定チケット"
    planned_tickets_admin.admin_order_field = "_planned_ticket_count"

    def current_tickets_admin(self, obj):
        try:
            return int(getattr(obj, "ticket_balance", 0) or 0)
        except Exception:
            return 0

    current_tickets_admin.short_description = "現在の保有チケット"
    current_tickets_admin.admin_order_field = "ticket_balance"

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        now = timezone.now()

        # 通常予約と固定予約で同じ Reservation を集計の正本にする。
        consumed_subquery = (
            Reservation.objects.filter(
                user_id=OuterRef("pk"),
                start_at__lte=now,
                status__in=VALID_RESERVATION_STATUSES,
            )
            .values("user_id")
            .annotate(total=Sum("tickets_used"))
            .values("total")[:1]
        )
        planned_subquery = (
            Reservation.objects.filter(
                user_id=OuterRef("pk"),
                start_at__gt=now,
                status__in=VALID_RESERVATION_STATUSES,
            )
            .values("user_id")
            .annotate(total=Sum("tickets_used"))
            .values("total")[:1]
        )

        return queryset.annotate(
            _consumed_ticket_count=Coalesce(
                Subquery(consumed_subquery, output_field=IntegerField()),
                Value(0),
            ),
            _planned_ticket_count=Coalesce(
                Subquery(planned_subquery, output_field=IntegerField()),
                Value(0),
            ),
        )
