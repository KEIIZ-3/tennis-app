"""One-off repair for the twelve user-confirmed legacy reservations."""

from dataclasses import asdict, dataclass
from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Reservation, TicketConsumption
from .settlement_models import MonthlySettlement


class ConfirmedLegacyRepairRejected(ValidationError):
    pass


@dataclass(frozen=True)
class ConfirmedTarget:
    member_name: str
    lesson_date: date
    price: int


CONFIRMED_TARGETS = {
    1491: ConfirmedTarget("矢野充則", date(2026, 8, 2), 0),
    1535: ConfirmedTarget("佐野新一", date(2026, 8, 20), 0),
    1527: ConfirmedTarget("赤木 琴江", date(2026, 8, 23), 0),
    1536: ConfirmedTarget("土岩 優羽", date(2026, 8, 19), 3500),
    1548: ConfirmedTarget("赤木 琴江", date(2026, 9, 20), 3500),
    1549: ConfirmedTarget("赤木 琴江", date(2026, 9, 27), 3500),
    1633: ConfirmedTarget("内藤 理恵", date(2026, 9, 29), 3500),
    1634: ConfirmedTarget("平馬 浩子", date(2026, 9, 29), 3500),
    1660: ConfirmedTarget("上田 美恵", date(2026, 10, 17), 3500),
    1661: ConfirmedTarget("桜井 佳苗", date(2026, 10, 17), 3500),
    1662: ConfirmedTarget("篠原早織", date(2026, 10, 17), 3500),
    1653: ConfirmedTarget("桜井 佳苗", date(2026, 10, 28), 3500),
}


@dataclass(frozen=True)
class ConfirmedRepairRow:
    reservation_id: int
    member_name: str
    lesson_date: str
    current_snapshot: int | None
    new_snapshot: int
    current_consumptions: list
    planned_consumption_change: str
    would_change: bool
    validation_result: str

    def to_dict(self):
        return asdict(self)


def _member_name(reservation):
    return reservation.user.full_name or reservation.user.username


def _normalize_member_name(value):
    return "".join(character for character in value if not character.isspace())


def _consumption_values(rows):
    return [
        {
            "id": row.id,
            "user_id": row.user_id,
            "purchase_id": row.purchase_id,
            "tickets_used": row.tickets_used,
            "unit_price_snapshot": row.unit_price_snapshot,
            "refunded_at": row.refunded_at.isoformat() if row.refunded_at else None,
        }
        for row in rows
    ]


def _inspect_one(reservation, target):
    rows = list(reservation.ticket_consumptions.order_by("created_at", "id"))
    current = _consumption_values(rows)
    local_date = timezone.localtime(reservation.start_at).date()
    errors = []
    if _normalize_member_name(_member_name(reservation)) != _normalize_member_name(
        target.member_name
    ):
        errors.append("member_name_mismatch")
    if local_date != target.lesson_date:
        errors.append("lesson_date_mismatch")
    if reservation.tickets_used != 1:
        errors.append("tickets_used_not_one")
    if reservation.status != Reservation.STATUS_ACTIVE:
        errors.append("reservation_not_active")

    active = [row for row in rows if row.refunded_at is None]
    exact_consumption = (
        len(rows) == 1
        and len(active) == 1
        and active[0].user_id == reservation.user_id
        and active[0].tickets_used == 1
        and active[0].unit_price_snapshot == target.price
        and (
            target.price != 0
            or (
                active[0].purchase_id is not None
                and active[0].purchase.unit_price == 0
            )
        )
    )
    if reservation.participant_ticket_price_snapshot == target.price and exact_consumption:
        return ConfirmedRepairRow(
            reservation.id, _member_name(reservation), local_date.isoformat(),
            reservation.participant_ticket_price_snapshot, target.price, current,
            "none", False, "already_correct",
        )
    if reservation.participant_ticket_price_snapshot is not None:
        errors.append("snapshot_already_set")

    planned = "none"
    if target.price == 0:
        if not exact_consumption:
            errors.append("confirmed_zero_consumption_mismatch")
    elif not rows:
        planned = "create_purchase_null_consumption"
    elif len(rows) != 1 or len(active) != 1:
        errors.append("ambiguous_consumption_shape")
    else:
        row = active[0]
        if row.user_id != reservation.user_id or row.tickets_used != 1:
            errors.append("consumption_identity_mismatch")
        if row.purchase_id is not None:
            errors.append("existing_purchase_outside_confirmed_shape")
        if row.unit_price_snapshot is not None:
            errors.append("existing_consumption_price")
        if not errors:
            planned = "set_consumption_price_3500"

    return ConfirmedRepairRow(
        reservation.id, _member_name(reservation), local_date.isoformat(),
        reservation.participant_ticket_price_snapshot, target.price, current,
        planned, not errors, "ok" if not errors else ",".join(errors),
    )


def inspect_confirmed_legacy_prices(*, reservation_ids=None, lock=False):
    requested = set(CONFIRMED_TARGETS if reservation_ids is None else reservation_ids)
    unknown = requested - set(CONFIRMED_TARGETS)
    if unknown:
        raise ConfirmedLegacyRepairRejected(
            f"reservation_ids_outside_confirmed_scope={sorted(unknown)}"
        )
    queryset = Reservation.objects.select_related("user").filter(pk__in=requested)
    if lock:
        queryset = queryset.select_for_update()
    reservations = {row.id: row for row in queryset}
    results = []
    for reservation_id in sorted(requested):
        target = CONFIRMED_TARGETS[reservation_id]
        reservation = reservations.get(reservation_id)
        if reservation is None:
            results.append(ConfirmedRepairRow(
                reservation_id, target.member_name, target.lesson_date.isoformat(),
                None, target.price, [], "none", False, "reservation_missing",
            ))
        else:
            results.append(_inspect_one(reservation, target))
    return results


def summarize_confirmed_rows(rows):
    return {
        "target_count": len(rows),
        "zero_price_count": sum(row.new_snapshot == 0 for row in rows),
        "price_3500_count": sum(row.new_snapshot == 3500 for row in rows),
        "would_change_count": sum(row.would_change for row in rows),
        "already_correct_count": sum(row.validation_result == "already_correct" for row in rows),
        "blocked_count": sum(
            row.validation_result not in ("ok", "already_correct") for row in rows
        ),
    }


@transaction.atomic
def apply_confirmed_legacy_prices():
    list(Reservation.objects.select_for_update().filter(
        pk__in=CONFIRMED_TARGETS,
    ).order_by("id"))
    list(TicketConsumption.objects.select_for_update().filter(
        reservation_id__in=CONFIRMED_TARGETS,
    ).order_by("id"))
    rows = inspect_confirmed_legacy_prices()
    blocked = [row for row in rows if row.validation_result not in ("ok", "already_correct")]
    if blocked:
        details = [f"{row.reservation_id}:{row.validation_result}" for row in blocked]
        raise ConfirmedLegacyRepairRejected("; ".join(details))

    closed = list(MonthlySettlement.objects.select_for_update().filter(
        year=2026, month__in=(8, 9, 10), status=MonthlySettlement.STATUS_CLOSED,
    ).values_list("month", flat=True))
    if closed:
        raise ConfirmedLegacyRepairRejected(f"closed_months={sorted(closed)}")

    changed_months = set()
    for result in rows:
        if not result.would_change:
            continue
        reservation = Reservation.objects.get(pk=result.reservation_id)
        consumption = reservation.ticket_consumptions.first()
        if consumption is None:
            TicketConsumption.objects.create(
                user=reservation.user,
                purchase=None,
                reservation=reservation,
                fixed_lesson=reservation.fixed_lesson,
                tickets_used=reservation.tickets_used,
                unit_price_snapshot=result.new_snapshot,
                refunded_at=None,
            )
        elif result.planned_consumption_change == "set_consumption_price_3500":
            consumption.unit_price_snapshot = result.new_snapshot
            consumption.save(update_fields=["unit_price_snapshot"])
        updated = Reservation.objects.filter(
            pk=reservation.pk, participant_ticket_price_snapshot__isnull=True,
        ).update(participant_ticket_price_snapshot=result.new_snapshot)
        if updated != 1:
            raise ConfirmedLegacyRepairRejected(
                f"reservation_changed_during_apply={reservation.pk}"
            )
        changed_months.add((reservation.start_at.year, reservation.start_at.month))

    from .settlement_service import calculate_monthly_settlement
    draft_months = set(MonthlySettlement.objects.filter(
        year=2026,
        month__in=[month for year, month in changed_months if year == 2026],
        status=MonthlySettlement.STATUS_DRAFT,
    ).values_list("year", "month"))
    for year, month in sorted(changed_months & draft_months):
        calculate_monthly_settlement(year, month, force=True)
    return rows
