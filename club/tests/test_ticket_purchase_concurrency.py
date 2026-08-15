import threading

from django.db import close_old_connections, connection
from django.test import TransactionTestCase, skipUnlessDBFeature

from club.models import TicketLedger, TicketPurchase, User, purchase_tickets


class TicketPurchasePostgreSQLConcurrencyTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_same_idempotency_key_converges_to_one_purchase(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQLのrow lockとunique競合を検証する専用テストです。")

        member = User.objects.create_user(username="concurrent-ticket-member", ticket_balance=0)
        barrier = threading.Barrier(2)
        errors = []

        def buy():
            close_old_connections()
            try:
                thread_member = User.objects.get(pk=member.pk)
                barrier.wait()
                purchase_tickets(
                    user=thread_member,
                    tickets=4,
                    unit_price=3500,
                    purchase_type=TicketPurchase.PURCHASE_TYPE_SET4,
                    reason=TicketLedger.REASON_PURCHASE_SET4,
                    idempotency_key="concurrent-same-purchase",
                )
            except Exception as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=buy) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        member.refresh_from_db()
        self.assertEqual(member.ticket_balance, 4)
        self.assertEqual(TicketPurchase.objects.filter(user=member).count(), 1)
        self.assertEqual(TicketLedger.objects.filter(user=member).count(), 1)
