from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("club", "0055_guest_participation_accounting"),
    ]

    operations = [
        migrations.AlterField(
            model_name="stringingorder",
            name="assigned_coach",
            field=models.ForeignKey(
                blank=True,
                limit_choices_to={"role": "coach"},
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="assigned_stringing_orders",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
