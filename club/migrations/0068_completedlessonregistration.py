from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("club", "0067_fixed_lesson_canceled_occurrence")]
    operations = [
        migrations.CreateModel(
            name="CompletedLessonRegistration",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("idempotency_key", models.CharField(max_length=64, unique=True)),
                ("note", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("canceled_at", models.DateTimeField(blank=True, null=True)),
                ("availability", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="completed_registration", to="club.coachavailability")),
                ("canceled_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="canceled_completed_lesson_registrations", to=settings.AUTH_USER_MODEL)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="created_completed_lesson_registrations", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at", "-id"]},
        )
    ]
