from django.db import migrations
from django.contrib.auth.hashers import make_password


def create_auth_users(apps, schema_editor):
    User = apps.get_model("club", "User")
    TicketWallet = apps.get_model("club", "TicketWallet")

    users = [
        {
            "username": "admin",
            "email": "admin@example.com",
            "role": "customer",
            "is_staff": True,
            "is_superuser": True,
            "is_active": True,
            "color": "#2ecc71",
            "password": "Admin12345!",
            "ticket_balance": 0,
        },
        {
            "username": "member1",
            "email": "member1@example.com",
            "role": "customer",
            "is_staff": False,
            "is_superuser": False,
            "is_active": True,
            "color": "#2ecc71",
            "password": "Member12345!",
            "ticket_balance": 10,
        },
        {
            "username": "coach1",
            "email": "coach1@example.com",
            "role": "coach",
            "is_staff": False,
            "is_superuser": False,
            "is_active": True,
            "color": "#2ecc71",
            "password": "Coach12345!",
            "ticket_balance": 0,
        },
    ]

    for item in users:
        user, created = User.objects.get_or_create(
            username=item["username"],
            defaults={
                "email": item["email"],
                "role": item["role"],
                "is_staff": item["is_staff"],
                "is_superuser": item["is_superuser"],
                "is_active": item["is_active"],
                "color": item["color"],
                "password": make_password(item["password"]),
            },
        )

        if not created:
            changed = False

            if user.email != item["email"]:
                user.email = item["email"]
                changed = True
            if user.role != item["role"]:
                user.role = item["role"]
                changed = True
            if user.is_staff != item["is_staff"]:
                user.is_staff = item["is_staff"]
                changed = True
            if user.is_superuser != item["is_superuser"]:
                user.is_superuser = item["is_superuser"]
                changed = True
            if user.is_active != item["is_active"]:
                user.is_active = item["is_active"]
                changed = True
            if getattr(user, "color", "") != item["color"]:
                user.color = item["color"]
                changed = True

            # 既存ユーザーでも今回の検証用にパスワードを揃える
            user.password = make_password(item["password"])
            changed = True

            if changed:
                user.save()

        wallet, _ = TicketWallet.objects.get_or_create(user=user)
        if wallet.balance != item["ticket_balance"]:
            wallet.balance = item["ticket_balance"]
            wallet.save(update_fields=["balance", "updated_at"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0009_alter_court_options_alter_reservation_options_and_more"),
    ]

    operations = [
        migrations.RunPython(create_auth_users, noop_reverse),
    ]
