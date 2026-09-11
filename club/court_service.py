from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import CoachAvailability, Court


def validate_court_capacity(*, court, available_court_count):
    """Use the same overlapping occurrence/court_count source as lesson validation."""
    requested = int(available_court_count or 0)
    occurrences = CoachAvailability.objects.filter(
        court=court,
        end_at__gte=timezone.now(),
    ).order_by("start_at", "pk")
    for occurrence in occurrences:
        used = occurrences.filter(
            start_at__lt=occurrence.end_at,
            end_at__gt=occurrence.start_at,
        ).aggregate(total=Sum("court_count"))["total"] or 0
        if used > requested:
            raise ValidationError(
                f"既存の開催回が同時に{used}面を使用するため、利用可能コート面数を{requested}面へ変更できません。"
            )


def validate_court_update(*, current, candidate):
    candidate.full_clean()
    if current is None:
        return
    if candidate.court_type != current.court_type and CoachAvailability.objects.filter(court=current).exists():
        raise ValidationError("開催履歴があるコートの種別は変更できません。")
    if candidate.available_court_count < current.available_court_count:
        validate_court_capacity(court=current, available_court_count=candidate.available_court_count)


@transaction.atomic
def save_court_update(*, candidate):
    current = None
    if candidate.pk:
        current = Court.objects.select_for_update().get(pk=candidate.pk)
    validate_court_update(current=current, candidate=candidate)
    candidate.save()
    return candidate
