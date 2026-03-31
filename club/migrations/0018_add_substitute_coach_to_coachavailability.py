from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0017_substitute_coach_and_general_lesson_scale"),
    ]

    operations = [
        migrations.AddField(
            model_name="coachavailability",
            name="substitute_coach",
            field=models.ForeignKey(
                blank=True,
                limit_choices_to={"role": "coach"},
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="substitute_coach_availabilities",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
