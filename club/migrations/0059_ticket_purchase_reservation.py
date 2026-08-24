from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("club", "0058_coachavailability_coach_2")]
    operations = [
        migrations.AddField(model_name="ticketpurchase", name="expires_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.CreateModel(
            name="TicketPurchaseReservation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("purchase_type", models.CharField(choices=[("single", "1枚購入"), ("set4", "4枚セット"), ("event", "イベント用"), ("admin", "管理画面調整"), ("formal_free", "無料謝礼"), ("legacy", "旧データ移行")], max_length=20)),
                ("ticket_count", models.PositiveIntegerField()), ("unit_price", models.PositiveIntegerField()), ("total_amount", models.PositiveIntegerField()),
                ("status", models.CharField(choices=[("pending", "承認待ち"), ("approved", "購入完了"), ("canceled", "キャンセル")], default="pending", max_length=20)),
                ("requested_at", models.DateTimeField(auto_now_add=True)), ("approved_at", models.DateTimeField(blank=True, null=True)), ("canceled_at", models.DateTimeField(blank=True, null=True)),
                ("approved_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="approved_ticket_purchase_reservations", to=settings.AUTH_USER_MODEL)),
                ("ticket_purchase", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="purchase_reservation", to="club.ticketpurchase")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="ticket_purchase_reservations", to=settings.AUTH_USER_MODEL)),
            ], options={"ordering": ["-requested_at", "-id"]},
        ),
    ]
