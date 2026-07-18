import jpholiday
from django import template
from django.utils import timezone

from club.court_fee_service import calculate_availability_court_fee

register = template.Library()


@register.filter
def is_japanese_holiday(value):
    if not value:
        return False
    try:
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        target_date = value.date() if hasattr(value, "date") else value
        return bool(jpholiday.is_holiday(target_date))
    except Exception:
        return False


@register.simple_tag
def court_fee_quote(availability):
    try:
        return calculate_availability_court_fee(availability)
    except Exception:
        return None
