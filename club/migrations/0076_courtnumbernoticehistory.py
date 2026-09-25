from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("club", "0075_coach_availability_attribute_overrides"),
    ]

    operations = [
        migrations.CreateModel(
            name="CourtNumberNoticeHistory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("start_at", models.DateTimeField()),
                ("end_at", models.DateTimeField()),
                ("court_number", models.CharField(max_length=120)),
                ("line_sent_count", models.PositiveIntegerField(default=0)),
                ("email_sent_count", models.PositiveIntegerField(default=0)),
                ("undelivered_count", models.PositiveIntegerField(default=0)),
                ("sent_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("message_digest", models.CharField(max_length=64)),
                ("availability", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="court_number_notice_histories", to="club.coachavailability")),
                ("fixed_lesson", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="court_number_notice_histories", to="club.fixedlesson")),
                ("sent_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="court_number_notice_histories", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ("-sent_at", "-id"),
                "indexes": [
                    models.Index(fields=["fixed_lesson", "start_at", "end_at"], name="club_courtn_fixed_l_f147b8_idx"),
                    models.Index(fields=["availability", "start_at", "end_at"], name="club_courtn_availab_4b886a_idx"),
                ],
            },
        ),
    ]
