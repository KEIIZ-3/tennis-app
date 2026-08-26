from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("club", "0062_ticket_cash_receipt")]

    operations = [
        migrations.AddField(
            model_name="ticketpurchase",
            name="corrected_from",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="corrected_to",
                to="club.ticketpurchase",
            ),
        ),
        migrations.AddField(
            model_name="ticketpurchase",
            name="correction_reason",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
