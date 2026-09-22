# Legacy ticket accounting repair

`repair_legacy_ticket_accounting` reconstructs reservation ticket lots from
persisted `TicketPurchase` and `TicketLedger` events in chronological FIFO
order. It is intentionally separate from the older one-off linkage repairs.

The command is read-only by default and starts at 2026-08-01 JST. Its output
scope is limited to active member reservations that used tickets but still have
no `participant_ticket_price_snapshot`. Reservations with an existing snapshot,
guests, canceled reservations, and rain-canceled reservations are excluded.
Canceled and refunded ledger events remain part of each member's FIFO history.

```powershell
python manage.py repair_legacy_ticket_accounting
python manage.py repair_legacy_ticket_accounting --from-date 2026-08-01 --to-date 2026-10-31
python manage.py repair_legacy_ticket_accounting --reservation-id 1548
```

Each JSON row includes the reservation, member, coach, current snapshot and
consumptions, reconstructed purchase lots and unit prices, decision reason, and
`repair_status`. April and May 2026 are always reported as
`excluded_test_period` when explicitly included. Closed settlement months are
reported as `skipped_closed_month`.

A row is repairable only with one exact `reservation_use` ledger and a complete,
internally consistent FIFO history. Missing supply, manual/unknown balances,
purchase reversals, inconsistent refunds, uncertain legacy zero-price lots, and
conflicting existing evidence remain `ambiguous`. A zero-price lot is accepted
only when it is explicitly typed `formal_free`.

After reviewing the dry-run output, apply only with explicit approval:

```powershell
python manage.py repair_legacy_ticket_accounting --reservation-id 1548 --apply
```

Apply updates or creates only `TicketConsumption` evidence and sets
`Reservation.participant_ticket_price_snapshot` to the sum of
`unit_price_snapshot * tickets_used`. It does not alter ticket balances,
purchase remaining counts, ledgers, reservation execution status, or closed
months. Each affected draft month is passed once to
`calculate_monthly_settlement(..., force=True)`, whose canonical chain refreshes
later saved draft carry values.
