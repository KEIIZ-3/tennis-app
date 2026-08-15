from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("club", "0051_reservation_participant_ticket_price_snapshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticketpurchase",
            name="idempotency_key",
            field=models.CharField(blank=True, max_length=255, null=True, unique=True),
        ),
    ]
