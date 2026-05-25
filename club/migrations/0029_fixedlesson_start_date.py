# Generated manually for tennis-app

from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0028_fixedlesson_coach_2_fixedlesson_coach_3"),
    ]

    operations = [
        migrations.AddField(
            model_name="fixedlesson",
            name="start_date",
            field=models.DateField(
                default=timezone.localdate,
                verbose_name="繰り返し開始日",
            ),
        ),
    ]
