from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("club", "0077_group_ticket_rate_snapshots")]

    operations = [
        migrations.AlterField(
            model_name="coachavailability",
            name="court",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="coach_availabilities",
                to="club.court",
            ),
        ),
        migrations.AddField(
            model_name="coachavailability",
            name="custom_court_name",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AlterField(
            model_name="reservation",
            name="court",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="reservations",
                to="club.court",
            ),
        ),
        migrations.AddField(
            model_name="reservation",
            name="custom_court_name",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
