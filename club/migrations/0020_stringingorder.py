from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0019_schedulesurveyresponse"),
    ]

    operations = [
        migrations.CreateModel(
            name="StringingOrder",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("racket_name", models.CharField(blank=True, default="", max_length=120)),
                ("string_name", models.CharField(blank=True, default="", max_length=120)),
                ("delivery_requested", models.BooleanField(default=False)),
                ("delivery_location", models.CharField(blank=True, default="", max_length=255)),
                ("preferred_delivery_time", models.CharField(blank=True, default="", max_length=255)),
                ("note", models.TextField(blank=True, default="")),
                ("base_price", models.PositiveIntegerField(default=1200)),
                ("delivery_fee", models.PositiveIntegerField(default=0)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("requested", "受付済み"),
                            ("in_progress", "対応中"),
                            ("completed", "完了"),
                            ("canceled", "キャンセル"),
                        ],
                        default="requested",
                        max_length=20,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=models.CASCADE,
                        related_name="stringing_orders",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "ガット貼り依頼",
                "verbose_name_plural": "ガット貼り依頼",
                "ordering": ["-created_at", "-id"],
            },
        ),
    ]
