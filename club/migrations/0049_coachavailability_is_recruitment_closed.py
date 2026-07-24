from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0048_alter_stringingorder_assigned_coach"),
    ]

    operations = [
        migrations.AddField(
            model_name="coachavailability",
            name="is_recruitment_closed",
            field=models.BooleanField(default=False, verbose_name="参加者募集を終了"),
        ),
    ]
