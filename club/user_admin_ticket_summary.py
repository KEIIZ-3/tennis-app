from types import MethodType

from django.contrib import admin
from django.db.models import IntegerField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import Reservation, User


VALID_RESERVATION_STATUSES = (
    Reservation.STATUS_ACTIVE,
    Reservation.STATUS_PENDING,
)


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


def apply_user_admin_ticket_summary():
    model_admin = admin.site._registry.get(User)
    if model_admin is None:
        return

    model_admin.consumed_tickets_admin = MethodType(
        consumed_tickets_admin,
        model_admin,
    )
    model_admin.planned_tickets_admin = MethodType(
        planned_tickets_admin,
        model_admin,
    )
    model_admin.current_tickets_admin = MethodType(
        current_tickets_admin,
        model_admin,
    )

    current_display = list(model_admin.list_display or ())
    replacement_display = []
    inserted = False

    for field_name in current_display:
        if field_name == "ticket_balance":
            replacement_display.extend(
                (
                    "consumed_tickets_admin",
                    "planned_tickets_admin",
                    "current_tickets_admin",
                )
            )
            inserted = True
            continue
        replacement_display.append(field_name)

    if not inserted:
        replacement_display.extend(
            (
                "consumed_tickets_admin",
                "planned_tickets_admin",
                "current_tickets_admin",
            )
        )

    model_admin.list_display = tuple(dict.fromkeys(replacement_display))

    original_get_queryset = model_admin.get_queryset

    def get_queryset(self, request):
        queryset = original_get_queryset(request)
        now = timezone.now()

        consumed_filter = Q(
            ticket_consumptions__refunded_at__isnull=True,
        ) & (
            Q(ticket_consumptions__reservation__isnull=True)
            | Q(
                ticket_consumptions__reservation__start_at__lte=now,
                ticket_consumptions__reservation__status__in=VALID_RESERVATION_STATUSES,
            )
        )
        planned_filter = Q(
            ticket_consumptions__refunded_at__isnull=True,
            ticket_consumptions__reservation__start_at__gt=now,
            ticket_consumptions__reservation__status__in=VALID_RESERVATION_STATUSES,
        )

        return queryset.annotate(
            _consumed_ticket_count=Coalesce(
                Sum(
                    "ticket_consumptions__tickets_used",
                    filter=consumed_filter,
                ),
                Value(0),
                output_field=IntegerField(),
            ),
            _planned_ticket_count=Coalesce(
                Sum(
                    "ticket_consumptions__tickets_used",
                    filter=planned_filter,
                ),
                Value(0),
                output_field=IntegerField(),
            ),
        )

    model_admin.get_queryset = MethodType(get_queryset, model_admin)


apply_user_admin_ticket_summary()
