from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0020_stringingorder"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="stringingorder",
            name="assigned_coach",
            field=models.ForeignKey(
                blank=True,
                limit_choices_to={"role": "coach"},
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name="assigned_stringing_orders",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
