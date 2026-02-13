from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Court",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=50, unique=True)),
                ("is_active", models.BooleanField(default=True)),
            ],
        ),
        migrations.CreateModel(
            name="Reservation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("date", models.DateField()),
                ("start_time", models.TimeField()),
                ("end_time", models.TimeField()),
                ("status", models.CharField(choices=[("booked", "Booked"), ("cancelled", "Cancelled")], default="booked", max_length=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("court", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reservations", to="club.court")),
                ("customer", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="reservations", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-date", "-start_time"],
            },
        ),
        migrations.AddConstraint(
            model_name="reservation",
            constraint=models.UniqueConstraint(fields=("court", "date", "start_time"), name="uniq_reservation_court_date_start"),
        ),
    ]
