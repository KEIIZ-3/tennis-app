from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("club", "0068_completedlessonregistration"),
    ]
    operations = [
        migrations.AddField(
            model_name="shoppurchase", name="accounting_configured",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="shoppurchase", name="procurement_coach",
            field=models.ForeignKey(blank=True, null=True,
                limit_choices_to={"role": "coach"},
                on_delete=django.db.models.deletion.PROTECT,
                related_name="procured_shop_purchases", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="shoppurchase", name="profit_amount_snapshot",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="shoppurchase", name="profit_rate_snapshot",
            field=models.DecimalField(blank=True, null=True, max_digits=7, decimal_places=3),
        ),
    ]
