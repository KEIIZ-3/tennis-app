from django.core.management.base import BaseCommand

from club.ticket_state_snapshot import (
    build_ticket_state_snapshot,
    serialize_ticket_state_snapshot,
)


class Command(BaseCommand):
    help = "現在のチケット状態をREAD ONLYの決定的JSONとしてstdoutへ出力します。"

    def handle(self, *args, **options):
        self.stdout.write(serialize_ticket_state_snapshot(build_ticket_state_snapshot()))
