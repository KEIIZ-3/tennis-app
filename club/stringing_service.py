from django.core.exceptions import ValidationError
from datetime import datetime, time

from django.db import transaction
from django.utils import timezone

from .models import (
    STRINGING_BASE_PRICE,
    STRINGING_DELIVERY_FEE,
    StringingOrder,
    ensure_accounting_month_is_open,
)


def recognized_stringing_orders(queryset, *, month_start, next_month):
    """Return the single source used for monthly stringing revenue."""
    return queryset.filter(
        created_at__date__gte=month_start,
        created_at__date__lt=next_month,
    ).exclude(status=StringingOrder.STATUS_CANCELED)


def is_stringing_revenue_recognized(order):
    return getattr(order, "status", None) != StringingOrder.STATUS_CANCELED


def stringing_revenue_amount(order):
    if not is_stringing_revenue_recognized(order):
        return 0
    return int(order.total_price())


@transaction.atomic
def create_stringing_order(*, order, user):
    """Validate and persist a customer order as one business operation."""
    order.user = user
    order.status = StringingOrder.STATUS_REQUESTED
    order.base_price = STRINGING_BASE_PRICE
    order.delivery_fee = (
        STRINGING_DELIVERY_FEE if order.delivery_requested else 0
    )
    order.full_clean()
    order.save()
    return order


@transaction.atomic
def create_recorded_stringing_order(*, order, user, assigned_coach, performed_date):
    """Persist a completed oral-request record using the existing accounting source."""
    ensure_accounting_month_is_open(performed_date)
    order.user = user
    order.assigned_coach = assigned_coach
    order.status = StringingOrder.STATUS_COMPLETED
    order.base_price = STRINGING_BASE_PRICE
    order.delivery_fee = (
        STRINGING_DELIVERY_FEE if order.delivery_requested else 0
    )
    order.full_clean()
    order.save()

    performed_at = timezone.make_aware(
        datetime.combine(performed_date, time(12, 0)),
        timezone.get_current_timezone(),
    )
    StringingOrder.objects.filter(pk=order.pk).update(created_at=performed_at)
    order.created_at = performed_at
    return order


@transaction.atomic
def update_stringing_order_status(*, order_id, new_status):
    valid_statuses = {value for value, _label in StringingOrder.STATUS_CHOICES}
    if new_status not in valid_statuses:
        raise ValidationError("更新する状態が不正です。")

    order = (
        StringingOrder.objects.select_for_update()
        .select_related("user", "assigned_coach")
        .get(pk=order_id)
    )
    if order.status == new_status:
        return order, False

    order.status = new_status
    order.full_clean()
    order.save(update_fields=["status", "updated_at"])
    return order, True
