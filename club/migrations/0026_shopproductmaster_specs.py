from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0025_shopproductmaster"),
    ]

    operations = [
        migrations.AddField(
            model_name="shopproductmaster",
            name="spec_balance",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="shopproductmaster",
            name="spec_beam",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="shopproductmaster",
            name="spec_gauge",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="shopproductmaster",
            name="spec_head_size",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="shopproductmaster",
            name="spec_length",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="shopproductmaster",
            name="spec_set_length",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="shopproductmaster",
            name="spec_string_pattern",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="shopproductmaster",
            name="spec_weight_unstrung",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
    ]
