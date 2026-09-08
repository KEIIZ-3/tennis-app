from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("club", "0071_shop_guest_buyers")]

    operations = [
        migrations.AlterField(
            model_name="shoppurchase", name="quote",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="purchases", to="club.shopquote",
            ),
        ),
        migrations.AlterField(
            model_name="shoppurchase", name="status",
            field=models.CharField(
                choices=[("confirmed", "購入確定"), ("canceled", "取消"),
                         ("reverted", "差し戻し済み")],
                default="confirmed", max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="shoprevenueallocationaudit", name="event_type",
            field=models.CharField(default="accounting", max_length=20),
        ),
        migrations.AddField(
            model_name="shoprevenueallocationaudit", name="reason",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddConstraint(
            model_name="shoppurchase",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "confirmed")), fields=("quote",),
                name="unique_confirmed_shop_purchase_per_quote",
            ),
        ),
    ]
