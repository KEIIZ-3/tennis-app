from django.db import migrations, models
from django.utils import timezone
import django.db.models.deletion


def backfill_fixed_lesson_provenance(apps, schema_editor):
    CoachAvailability = apps.get_model("club", "CoachAvailability")
    Reservation = apps.get_model("club", "Reservation")
    today = timezone.localdate()

    future_availability_ids = (
        Reservation.objects.filter(
            availability_id__isnull=False,
            fixed_lesson_id__isnull=False,
            start_at__date__gte=today,
        )
        .values_list("availability_id", flat=True)
        .distinct()
    )
    for availability in CoachAvailability.objects.filter(
        pk__in=future_availability_ids,
        start_at__date__gte=today,
    ).iterator():
        fixed_ids = list(
            Reservation.objects.filter(
                availability_id=availability.pk,
                fixed_lesson_id__isnull=False,
            )
            .values_list("fixed_lesson_id", flat=True)
            .distinct()[:2]
        )
        if len(fixed_ids) != 1:
            continue

        FixedLesson = apps.get_model("club", "FixedLesson")
        fixed_lesson = FixedLesson.objects.get(pk=fixed_ids[0])
        available_coach_count = 1 + int(availability.coach_2_id is not None)
        internally_consistent = availability.coach_count == available_coach_count
        matches_source = (
            availability.coach_id == fixed_lesson.coach_id
            and availability.coach_2_id == fixed_lesson.coach_2_id
            and availability.coach_count == fixed_lesson.coach_count
        )

        updates = {"fixed_lesson_source_id": fixed_lesson.pk}
        if matches_source:
            updates["coach_assignment_overridden"] = False
        elif internally_consistent:
            # A coherent differing assignment is treated as an intentional legacy override.
            updates["coach_assignment_overridden"] = True
        elif (
            availability.coach_count > available_coach_count
            and fixed_lesson.coach_count == availability.coach_count
        ):
            # The legacy sync copied the count without all coach foreign keys.
            updates.update({
                "coach_id": fixed_lesson.coach_id,
                "coach_2_id": fixed_lesson.coach_2_id,
                "coach_count": fixed_lesson.coach_count,
                "coach_assignment_overridden": False,
            })
        else:
            # Preserve an ambiguous legacy assignment instead of overwriting it.
            updates["coach_assignment_overridden"] = True

        CoachAvailability.objects.filter(pk=availability.pk).update(**updates)


class Migration(migrations.Migration):
    dependencies = [("club", "0072_shop_purchase_revisions")]

    operations = [
        migrations.AddField(
            model_name="coachavailability",
            name="coach_assignment_overridden",
            field=models.BooleanField(default=False, verbose_name="コーチ構成の個別変更"),
        ),
        migrations.AddField(
            model_name="coachavailability",
            name="fixed_lesson_source",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="generated_availabilities",
                to="club.fixedlesson",
                verbose_name="由来固定レッスン",
            ),
        ),
        migrations.RunPython(backfill_fixed_lesson_provenance, migrations.RunPython.noop),
    ]
