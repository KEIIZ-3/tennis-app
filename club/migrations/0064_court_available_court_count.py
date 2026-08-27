from django.db import migrations, models


def set_sono_available_court_count(apps, schema_editor):
    Court = apps.get_model("club", "Court")
    Court.objects.filter(name="西猪名公園").update(available_court_count=12)


class Migration(migrations.Migration):
    dependencies = [("club", "0063_ticket_purchase_correction")]

    operations = [
        migrations.AddField(
            model_name="court",
            name="available_court_count",
            field=models.PositiveIntegerField(default=2, verbose_name="利用可能コート面数"),
        ),
        migrations.RunPython(set_sono_available_court_count, migrations.RunPython.noop),
    ]
