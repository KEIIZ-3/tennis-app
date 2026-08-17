from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("club", "0053_alter_ticketpurchase_purchase_type")]

    operations = [
        migrations.AlterField(
            model_name="ticketconsumption",
            name="purchase",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="consumptions",
                to="club.ticketpurchase",
            ),
        ),
        migrations.AlterField(
            model_name="ticketconsumption",
            name="unit_price_snapshot",
            field=models.PositiveIntegerField(blank=True, default=None, null=True),
        ),
    ]
