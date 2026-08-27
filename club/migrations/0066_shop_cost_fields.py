from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("club", "0065_shopinquiry_shopquote_shoppurchase_shopquoteitem_and_more")]
    operations = [
        migrations.AddField(model_name="shopquoteitem", name="cost_price", field=models.PositiveIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="shoppurchase", name="cost_total", field=models.PositiveIntegerField(blank=True, null=True)),
    ]
