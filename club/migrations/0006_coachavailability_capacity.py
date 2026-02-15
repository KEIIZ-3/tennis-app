from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0005_align_models"),
    ]

    operations = [
        migrations.AddField(
            model_name="coachavailability",
            name="capacity",
            field=models.PositiveIntegerField(default=1),
        ),
    ]

