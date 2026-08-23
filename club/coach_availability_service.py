from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import CoachAvailability, ensure_accounting_month_is_open


def delete_single_lesson(*, availability_id, actor):
    with transaction.atomic():
        availability = CoachAvailability.objects.select_for_update().get(pk=availability_id)
        if not (
            getattr(actor, "is_staff", False)
            or getattr(actor, "is_superuser", False)
            or availability.includes_coach(actor)
        ):
            raise PermissionError
        ensure_accounting_month_is_open(availability.start_at)
        if availability.reservations.exists():
            raise ValidationError("予約履歴があるため削除できません。中止処理を使用してください。")
        local_start = timezone.localtime(availability.start_at) if timezone.is_aware(availability.start_at) else availability.start_at
        year, month = local_start.year, local_start.month
        availability.delete()
        return year, month
