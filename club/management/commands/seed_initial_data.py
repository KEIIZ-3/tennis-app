from __future__ import annotations

from datetime import time

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from club.models import BusinessHours, Court, TicketWallet

User = get_user_model()


class Command(BaseCommand):
    help = "新DB向けの初期データを投入します。"

    def handle(self, *args, **options):
        courts = ["Court A", "Court B"]
        for name in courts:
            court, created = Court.objects.get_or_create(name=name, defaults={"is_active": True})
            self.stdout.write(self.style.SUCCESS(f"{'created' if created else 'exists'} court: {court.name}"))

        for weekday in range(7):
            bh, created = BusinessHours.objects.get_or_create(
                weekday=weekday,
                defaults={
                    "open_time": time(9, 0),
                    "close_time": time(21, 0),
                    "is_closed": False,
                },
            )
            self.stdout.write(self.style.SUCCESS(f"{'created' if created else 'exists'} business hours: {bh.weekday}"))

        coach_specs = [
            ("coach1", "coach1@example.com", "#2ecc71"),
            ("coach2", "coach2@example.com", "#3498db"),
        ]
        for username, email, color in coach_specs:
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "email": email,
                    "role": "coach",
                    "is_active": True,
                },
            )
            changed = False
            if user.role != "coach":
                user.role = "coach"
                changed = True
            if user.color != color:
                user.color = color
                changed = True
            if changed:
                user.save()

            if created:
                user.set_password("changeme12345")
                user.save()

            self.stdout.write(self.style.SUCCESS(f"{'created' if created else 'exists'} coach: {user.username}"))

        customer_specs = [
            ("member1", "member1@example.com", 10),
            ("member2", "member2@example.com", 5),
        ]
        for username, email, balance in customer_specs:
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "email": email,
                    "role": "customer",
                    "is_active": True,
                },
            )
            if created:
                user.set_password("changeme12345")
                user.save()

            wallet, _ = TicketWallet.objects.get_or_create(user=user)
            if wallet.balance == 0:
                wallet.balance = balance
                wallet.save(update_fields=["balance", "updated_at"])

            self.stdout.write(self.style.SUCCESS(f"{'created' if created else 'exists'} customer: {user.username}"))

        self.stdout.write(self.style.SUCCESS("seed_initial_data completed"))
