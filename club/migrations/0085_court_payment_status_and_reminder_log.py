from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("club", "0084_courtbookinginfo"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="coachavailability",
            name="court_payment_status",
            field=models.CharField(
                blank=True,
                choices=[("", "未設定"), ("unpaid", "未払い")],
                default="",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="CourtPaymentReminderLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("target_date", models.DateField()),
                ("sent_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "recipient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="court_payment_reminder_logs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ("-target_date", "recipient_id")},
        ),
        migrations.AddConstraint(
            model_name="courtpaymentreminderlog",
            constraint=models.UniqueConstraint(
                fields=("target_date", "recipient"),
                name="unique_court_payment_reminder_recipient",
            ),
        ),
    ]
