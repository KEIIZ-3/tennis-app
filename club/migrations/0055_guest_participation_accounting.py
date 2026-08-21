from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("club", "0054_ticketconsumption_pending_evidence")]
    operations = [
        migrations.AlterField(
            model_name="reservation", name="user",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="reservations", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="reservation", name="guest_name",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.CreateModel(
            name="ParticipantPriceChange",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("participant_name", models.CharField(max_length=120)),
                ("old_amount", models.PositiveIntegerField()),
                ("new_amount", models.PositiveIntegerField()),
                ("changed_at", models.DateTimeField(auto_now_add=True)),
                ("changed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="participant_price_changes", to=settings.AUTH_USER_MODEL)),
                ("reservation", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="price_changes", to="club.reservation")),
            ],
            options={"ordering": ["-changed_at", "-id"]},
        ),
    ]
