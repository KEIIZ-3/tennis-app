from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("club", "0061_ticket_burden_change")]

    operations = [
        migrations.CreateModel(
            name="TicketCashReceipt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.PositiveIntegerField()),
                ("received_at", models.DateTimeField()),
                ("payment_method", models.CharField(choices=[("cash", "現金")], default="cash", max_length=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("idempotency_key", models.CharField(blank=True, max_length=255, null=True, unique=True)),
                ("reversed_at", models.DateTimeField(blank=True, null=True)),
                ("reversal_reason", models.CharField(blank=True, default="", max_length=255)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="created_ticket_cash_receipts", to=settings.AUTH_USER_MODEL)),
                ("reversed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reversed_ticket_cash_receipts", to=settings.AUTH_USER_MODEL)),
                ("ticket_purchase", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="cash_receipts", to="club.ticketpurchase")),
            ],
            options={"ordering": ["received_at", "id"]},
        ),
    ]
