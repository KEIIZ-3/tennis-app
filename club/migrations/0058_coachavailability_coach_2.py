from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("club", "0057_alter_participantpricechange_old_amount"),
    ]

    operations = [
        migrations.AddField(
            model_name="coachavailability",
            name="coach_2",
            field=models.ForeignKey(
                blank=True,
                limit_choices_to={"role__in": ("coach", "contractor_coach")},
                null=True,
                on_delete=models.SET_NULL,
                related_name="coach_availabilities_as_coach_2",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
