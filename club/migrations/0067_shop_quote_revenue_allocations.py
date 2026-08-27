from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("club", "0066_shop_cost_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="ShopQuoteRevenueAllocationAudit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("allocation_snapshot", models.JSONField(default=list)),
                ("changed_at", models.DateTimeField(auto_now_add=True)),
                ("changed_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="shop_quote_allocation_audits", to=settings.AUTH_USER_MODEL)),
                ("quote", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="planned_allocation_audits", to="club.shopquote")),
            ],
        ),
        migrations.CreateModel(
            name="ShopQuoteRevenueAllocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("coach", models.ForeignKey(limit_choices_to={"role__in": ("coach", "contractor_coach")}, on_delete=django.db.models.deletion.PROTECT, related_name="planned_shop_revenue_allocations", to=settings.AUTH_USER_MODEL)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="created_shop_quote_allocations", to=settings.AUTH_USER_MODEL)),
                ("quote", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="planned_allocations", to="club.shopquote")),
            ],
        ),
        migrations.AddConstraint(
            model_name="shopquoterevenueallocation",
            constraint=models.UniqueConstraint(fields=("quote", "coach"), name="unique_shop_quote_coach_allocation"),
        ),
    ]
