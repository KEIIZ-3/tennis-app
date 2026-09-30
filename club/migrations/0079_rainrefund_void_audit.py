from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("club", "0078_custom_court_names"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="rainrefund",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "返金待ち"),
                    ("refunded", "返金済み"),
                    ("voided", "取消済み"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="rainrefund",
            name="void_reason",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="rainrefund",
            name="voided_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="rainrefund",
            name="voided_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="voided_rain_refunds",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
