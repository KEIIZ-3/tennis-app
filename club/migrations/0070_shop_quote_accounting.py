from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("club", "0069_shop_purchase_accounting"),
    ]

    operations = [
        migrations.AddField(
            model_name="shopquote", name="accounting_sale_amount",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="shopquote", name="accounting_purchase_cost",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="shopquote", name="planned_profit_allocations",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="shopquote", name="procurement_coach",
            field=models.ForeignKey(blank=True, null=True, limit_choices_to={"role": "coach"},
                on_delete=django.db.models.deletion.PROTECT, related_name="procured_shop_quotes",
                to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name="shoprevenueallocationaudit", name="purchase",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="allocation_audits", to="club.shoppurchase"),
        ),
        migrations.AddField(
            model_name="shoprevenueallocationaudit", name="previous_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="shoprevenueallocationaudit", name="quote",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="accounting_audits", to="club.shopquote"),
        ),
    ]
