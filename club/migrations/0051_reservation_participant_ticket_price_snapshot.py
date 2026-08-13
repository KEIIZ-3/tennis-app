from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("club", "0050_settlement_period_and_rain_refund"),
    ]

    operations = [
        migrations.AddField(
            model_name="reservation",
            name="participant_ticket_price_snapshot",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
