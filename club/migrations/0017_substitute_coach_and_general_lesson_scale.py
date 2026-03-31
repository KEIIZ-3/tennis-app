from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0016_coachexpense"),
    ]

    operations = [
        migrations.AddField(
            model_name="coachavailability",
            name="coach_count",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="coachavailability",
            name="court_count",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="fixedlesson",
            name="coach_count",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="fixedlesson",
            name="court_count",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="reservation",
            name="substitute_coach",
            field=models.ForeignKey(
                blank=True,
                limit_choices_to={"role": "coach"},
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="substitute_reservations",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
