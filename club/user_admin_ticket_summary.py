from types import MethodType

from django.contrib import admin
from django.db.models import Case, F, IntegerField, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from .models import Reservation, TicketLedger, User


VALID_RESERVATION_STATUSES = (
    Reservation.STATUS_ACTIVE,
    Reservation.STATUS_PENDING,
)

CONSUMPTION_LEDGER_REASONS = (
    TicketLedger.REASON_RESERVATION_USE,
    TicketLedger.REASON_FIXED_USE,
    TicketLedger.REASON_CANCEL_REFUND,
    TicketLedger.REASON_RAIN_REFUND,
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
        if field_name in {
            "ticket_balance",
            "consumed_tickets_admin",
            "planned_tickets_admin",
            "current_tickets_admin",
        }:
            if not inserted:
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

        # 消費済みは、開始日時を迎えた予約に紐づく台帳だけを集計する。
        # 予約時点で先にチケット台帳へ消費が記録されるため、未来予約の台帳を
        # ここへ含めると「消費済み」と「消費予定」の両方へ重複計上される。
        # 予約に紐づかない旧データは、過去の消費履歴として引き続き対象とする。
        consumed_subquery = (
            TicketLedger.objects.filter(
                user_id=OuterRef("pk"),
                reason__in=CONSUMPTION_LEDGER_REASONS,
            )
            .filter(
                Q(reservation__isnull=True)
                | Q(reservation__start_at__lte=now)
            )
            .values("user_id")
            .annotate(
                total=Sum(
                    Case(
                        When(
                            reason__in=CONSUMPTION_LEDGER_REASONS,
                            then=-F("change_amount"),
                        ),
                        default=Value(0),
                        output_field=IntegerField(),
                    )
                )
            )
            .values("total")[:1]
        )

        # 消費予定は、現在より後に開始する有効・承認待ち予約だけを集計する。
        # TicketLedger は予約作成時点で更新されるため、未来予約については参照せず、
        # Reservation.tickets_used を正本として扱う。
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
            _consumed_ticket_count=Greatest(
                Coalesce(
                    Subquery(consumed_subquery, output_field=IntegerField()),
                    Value(0),
                ),
                Value(0),
            ),
            _planned_ticket_count=Coalesce(
                Subquery(planned_subquery, output_field=IntegerField()),
                Value(0),
            ),
        )

    model_admin.get_queryset = MethodType(get_queryset, model_admin)


apply_user_admin_ticket_summary()
