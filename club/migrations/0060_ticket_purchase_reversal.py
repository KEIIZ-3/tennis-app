from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("club", "0059_ticket_purchase_reservation")]
    operations = [
        migrations.AddField(model_name="ticketpurchase", name="reversed_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="ticketpurchase", name="reversed_by", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reversed_ticket_purchases", to=settings.AUTH_USER_MODEL)),
        migrations.AddField(model_name="ticketpurchase", name="reversal_reason", field=models.CharField(blank=True, default="", max_length=30)),
        migrations.AddField(model_name="ticketpurchasereservation", name="reversed_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="ticketpurchasereservation", name="reversed_by", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reversed_ticket_purchase_reservations", to=settings.AUTH_USER_MODEL)),
        migrations.AddField(model_name="ticketpurchasereservation", name="reversal_reason", field=models.CharField(blank=True, default="", max_length=30)),
        migrations.AddField(model_name="ticketpurchasereservation", name="approved_for_reservation", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="approved_ticket_purchase_reservations", to="club.reservation")),
        migrations.AlterField(model_name="ticketpurchasereservation", name="status", field=models.CharField(choices=[("pending", "承認待ち"), ("approved", "購入完了"), ("canceled", "キャンセル"), ("reversed", "承認取消済み")], default="pending", max_length=20)),
        migrations.AlterField(model_name="ticketledger", name="reason", field=models.CharField(choices=[("purchase_single", "チケット1枚購入"), ("purchase_set4", "4枚セット購入"), ("reservation_use", "通常予約で消費"), ("fixed_use", "固定レッスンで消費"), ("cancel_refund", "キャンセル返却"), ("rain_refund", "雨天中止返却"), ("admin_adjust", "管理画面調整"), ("purchase_reversal", "チケット購入承認取消")], max_length=30)),
    ]
