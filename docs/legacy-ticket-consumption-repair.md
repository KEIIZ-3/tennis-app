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

## Read-only purchase evidence audit

Run the production investigation set without repairing or writing ticket data:

```powershell
python manage.py audit_missing_ticket_purchase_evidence
```

The JSON includes the repair logic's purchase candidates, classification,
reservation ledger, user purchase/ledger/consumption timelines, and totals. The
command has no apply mode. Repeat `--reservation-id` to audit an explicit set,
including a missing/normal comparison such as reservations 1541 and 1554.
