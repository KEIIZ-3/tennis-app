from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("club", "0044_register_family_reservation_models")]

    operations = [
        migrations.AlterField(
            model_name="coachexpense",
            name="note",
            field=models.TextField(blank=True, default=""),
        ),
    ]
