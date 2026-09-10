from django.db import migrations, models
from django.utils import timezone


def preserve_existing_occurrence_differences(apps, schema_editor):
    CoachAvailability = apps.get_model("club", "CoachAvailability")
    today = timezone.localdate()
    for availability in CoachAvailability.objects.filter(
        fixed_lesson_source_id__isnull=False,
        start_at__date__gte=today,
    ).select_related("fixed_lesson_source").iterator():
        fixed = availability.fixed_lesson_source
        updates = {}
        if availability.capacity != fixed.capacity:
            updates["capacity_overridden"] = True
        if (availability.court_id, availability.court_count) != (fixed.court_id, fixed.court_count):
            updates["court_assignment_overridden"] = True
        if (availability.target_level, availability.target_level_2) != (fixed.target_level, fixed.target_level_2):
            updates["level_overridden"] = True
        if availability.lesson_type != fixed.lesson_type:
            updates["lesson_type_overridden"] = True
        expected_note = f"固定レッスン: {fixed.title or fixed.get_weekday_display()}"
        if availability.note != expected_note:
            updates["note_overridden"] = True
        if updates:
            CoachAvailability.objects.filter(pk=availability.pk).update(**updates)


class Migration(migrations.Migration):
    dependencies = [("club", "0074_reconcile_fixed_lesson_availability_provenance")]

    operations = [
        migrations.AddField(model_name="coachavailability", name="capacity_overridden", field=models.BooleanField(default=False, verbose_name="定員の個別変更")),
        migrations.AddField(model_name="coachavailability", name="court_assignment_overridden", field=models.BooleanField(default=False, verbose_name="コート構成の個別変更")),
        migrations.AddField(model_name="coachavailability", name="level_overridden", field=models.BooleanField(default=False, verbose_name="対象レベルの個別変更")),
        migrations.AddField(model_name="coachavailability", name="lesson_type_overridden", field=models.BooleanField(default=False, verbose_name="レッスン種別の個別変更")),
        migrations.AddField(model_name="coachavailability", name="note_overridden", field=models.BooleanField(default=False, verbose_name="メモの個別変更")),
        migrations.RunPython(preserve_existing_occurrence_differences, migrations.RunPython.noop),
    ]
