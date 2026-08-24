# Historical ticket consumption repair

`repair_historical_ticket_consumptions` restores missing evidence for the 15 approved
historical reservations. It does not replay ticket use.

The command is dry-run by default:

```powershell
python manage.py repair_historical_ticket_consumptions
```

Apply only after every row is `candidate` (or an idempotent `noop`):

```powershell
python manage.py repair_historical_ticket_consumptions --apply
```

Purchase-linked rows create one `TicketConsumption`, reduce only the selected
`TicketPurchase.remaining_tickets`, and snapshot the persisted purchase price.
Reservation 1525 is the sole approved exception: it creates a consumption with
`purchase=None` and a 3,500 yen snapshot without creating or depleting a purchase.

The service rejects missing or duplicate reservation-use ledgers, missing consumption
timestamps, insufficient lot capacity, conflicting price snapshots, unapproved rows,
FIFO evidence mismatches, and closed accounting months. Customer balance, ticket
ledger, wallet/settlement values, and court/payment fields are checked as invariants.

After apply, run:

```powershell
python manage.py audit_executed_reservation_ticket_integrity <YEAR> <MONTH>
python manage.py audit_ticket_consumptions <YEAR> <MONTH>
python manage.py audit_missing_ticket_purchase_evidence
```

To find legacy rows that already have the exact reservation-use ledger but lack
consumption evidence, run this SELECT-only diagnostic in production:

```powershell
python manage.py audit_missing_ticket_purchase_evidence --all-pending-evidence-candidates
```

After reviewing an explicit reservation ID, restore the current formal deferred
evidence (`purchase=NULL`, `unit_price_snapshot=NULL`) with a dry-run first:

```powershell
python manage.py repair_historical_ticket_consumptions --pending-evidence --reservation-id 1534
python manage.py repair_historical_ticket_consumptions --pending-evidence --reservation-id 1534 --apply
```

This mode rejects refunded or canceled reservations, a missing/duplicate/mismatched
reservation-use ledger, existing evidence, and closed months. It creates no purchase
and changes no balance, ledger, reservation ticket count, wallet, or settlement.
