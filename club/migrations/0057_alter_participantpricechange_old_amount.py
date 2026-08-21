from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("club", "0056_alter_stringingorder_assigned_coach"),
    ]

    operations = [
        migrations.AlterField(
            model_name="participantpricechange",
            name="old_amount",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
