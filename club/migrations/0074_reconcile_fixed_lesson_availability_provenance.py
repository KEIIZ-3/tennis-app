from django.db import migrations
from django.utils import timezone

from club.fixed_lesson_provenance import (
    RECONCILE_AMBIGUOUS_ASSIGNMENT,
    RECONCILE_COHERENT_OVERRIDE,
    classify_fixed_lesson_coach_assignment,
)


def reconcile_future_provenance(apps, schema_editor):
    CoachAvailability = apps.get_model("club", "CoachAvailability")
    FixedLesson = apps.get_model("club", "FixedLesson")
    Reservation = apps.get_model("club", "Reservation")
    today = timezone.localdate()

    candidate_ids = (
        Reservation.objects.filter(
            availability_id__isnull=False,
            fixed_lesson_id__isnull=False,
            availability__fixed_lesson_source_id__isnull=True,
            availability__start_at__date__gte=today,
        )
        .order_by("availability_id")
        .values_list("availability_id", flat=True)
        .distinct()
    )
    for availability in CoachAvailability.objects.filter(pk__in=candidate_ids).iterator():
        fixed_ids = list(
            Reservation.objects.filter(
                availability_id=availability.pk,
                fixed_lesson_id__isnull=False,
            )
            .order_by("fixed_lesson_id")
            .values_list("fixed_lesson_id", flat=True)
            .distinct()[:2]
        )
        if len(fixed_ids) != 1:
            continue

        fixed_lesson = FixedLesson.objects.get(pk=fixed_ids[0])
        classification = classify_fixed_lesson_coach_assignment(
            availability_coach_id=availability.coach_id,
            availability_coach_2_id=availability.coach_2_id,
            availability_coach_count=availability.coach_count,
            fixed_lesson_coach_id=fixed_lesson.coach_id,
            fixed_lesson_coach_2_id=fixed_lesson.coach_2_id,
            fixed_lesson_coach_count=fixed_lesson.coach_count,
        )
        overridden = classification in {
            RECONCILE_COHERENT_OVERRIDE,
            RECONCILE_AMBIGUOUS_ASSIGNMENT,
        }
        updates = {
            "fixed_lesson_source_id": fixed_lesson.pk,
            "coach_assignment_overridden": overridden,
        }
        if not overridden:
            updates.update(
                coach_id=fixed_lesson.coach_id,
                coach_2_id=fixed_lesson.coach_2_id,
                coach_count=fixed_lesson.coach_count,
            )
        CoachAvailability.objects.filter(pk=availability.pk).update(**updates)


class Migration(migrations.Migration):
    dependencies = [("club", "0073_coach_availability_fixed_lesson_provenance")]

    operations = [
        migrations.RunPython(reconcile_future_provenance, migrations.RunPython.noop),
    ]
