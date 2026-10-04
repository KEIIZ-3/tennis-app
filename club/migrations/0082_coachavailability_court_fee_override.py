from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("club", "0081_coachavailability_court_accounting_defaults")]

    operations = [
        migrations.AddField(
            model_name="coachavailability",
            name="court_fee_amount",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="coachavailability",
            name="court_fee_overridden",
            field=models.BooleanField(default=False),
        ),
    ]
