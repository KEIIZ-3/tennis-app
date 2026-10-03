from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("club", "0079_rainrefund_void_audit"),
    ]

    operations = [
        migrations.AlterField(
            model_name="rainrefund",
            name="payer_coach",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="rain_refund_reimbursements",
                to="club.user",
            ),
        ),
    ]
