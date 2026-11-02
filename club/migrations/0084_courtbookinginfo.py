from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def migrate_existing_booking_info(apps, schema_editor):
    CoachAvailability = apps.get_model("club", "CoachAvailability")
    CourtBookingInfo = apps.get_model("club", "CourtBookingInfo")
    for availability in CoachAvailability.objects.exclude(
        court_reservation_number="",
        court_booking_account_kind="",
        court_booking_account_coach__isnull=True,
        court_booking_account_other="",
    ).iterator():
        CourtBookingInfo.objects.create(
            availability_id=availability.pk,
            position=1,
            reservation_number=availability.court_reservation_number,
            booking_account_kind=availability.court_booking_account_kind,
            booking_account_coach_id=availability.court_booking_account_coach_id,
            booking_account_other=availability.court_booking_account_other,
        )


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("club", "0083_coachavailability_court_reservation_number"),
    ]

    operations = [
        migrations.CreateModel(
            name="CourtBookingInfo",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("position", models.PositiveIntegerField()),
                ("reservation_number", models.CharField(blank=True, default="", max_length=10)),
                ("booking_account_kind", models.CharField(blank=True, choices=[("", "未設定"), ("coach", "コーチ"), ("other", "その他")], default="", max_length=20)),
                ("booking_account_other", models.CharField(blank=True, default="", max_length=255)),
                ("availability", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="court_booking_infos", to="club.coachavailability")),
                ("booking_account_coach", models.ForeignKey(blank=True, limit_choices_to={"role__in": ("coach", "contractor_coach")}, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="court_booking_infos", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["position", "id"]},
        ),
        migrations.AddConstraint(
            model_name="courtbookinginfo",
            constraint=models.UniqueConstraint(fields=("availability", "position"), name="unique_court_booking_position"),
        ),
        migrations.AddConstraint(
            model_name="courtbookinginfo",
            constraint=models.CheckConstraint(condition=models.Q(("position__gte", 1)), name="court_booking_position_gte_1"),
        ),
        migrations.RunPython(migrate_existing_booking_info, migrations.RunPython.noop),
        migrations.RemoveField(model_name="coachavailability", name="court_reservation_number"),
        migrations.RemoveField(model_name="coachavailability", name="court_booking_account_kind"),
        migrations.RemoveField(model_name="coachavailability", name="court_booking_account_coach"),
        migrations.RemoveField(model_name="coachavailability", name="court_booking_account_other"),
    ]
