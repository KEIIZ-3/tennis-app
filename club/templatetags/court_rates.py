import jpholiday
from django import template
from django.utils import timezone

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
