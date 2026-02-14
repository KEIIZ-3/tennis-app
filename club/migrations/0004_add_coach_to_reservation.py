from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0003_coachavailability"),
    ]

    operations = [
        migrations.AddField(
            model_name="reservation",
            name="coach",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="coach_reservations",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
