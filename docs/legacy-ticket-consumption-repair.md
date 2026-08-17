# Historical TicketConsumption linkage repair

`repair_legacy_ticket_consumptions` restores only missing historical
`TicketConsumption` linkage. It never replays `Reservation.consume_tickets()`,
changes `User.ticket_balance`, creates or edits a `TicketLedger`, changes
`TicketPurchase.remaining_tickets`, or recalculates wallet/court settlement data.

Only explicitly supplied reservations are inspected. A reservation is eligible
only when it has `ticket_consumed_at`, exactly one matching
`reservation_use` ledger, no existing consumption, a unique persisted purchase
lot whose unexplained historical depletion matches `tickets_used`, and matching
positive price evidence on both the purchase and reservation.

Dry-run is the default:

```powershell
python manage.py repair_legacy_ticket_consumptions --reservation-id 101 --reservation-id 102
```

Review every output row before applying. Any rejected row makes the command fail.
Apply only the same reviewed explicit IDs:

```powershell
python manage.py repair_legacy_ticket_consumptions --reservation-id 101 --reservation-id 102 --apply
```

Each apply runs atomically with row locks. After creating the linkage it verifies
ticket balance, ledger count and delta, purchase remaining tickets, reservation
payment/court fields, and the applicable monthly settlement snapshot. Any change
causes a rollback. Existing consumption is an idempotent no-op.
