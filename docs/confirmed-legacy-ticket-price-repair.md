# Confirmed legacy ticket price repair

`repair_confirmed_legacy_ticket_prices` is a one-off rescue command for exactly
twelve reservations left behind before the deferred FIFO workflow was introduced.
It is not called by reservation creation, ticket purchase, or deferred allocation.

The confirmed prices are embedded with each reservation ID, member name, and
lesson date: reservations 1491, 1535, and 1527 are 0 yen; reservations 1536,
1548, 1549, 1633, 1634, 1660, 1661, 1662, and 1653 are 3,500 yen. The default
operation is a read-only JSON dry-run:

```powershell
python manage.py repair_confirmed_legacy_ticket_prices
```

An individual confirmed ID can be inspected with `--reservation-id`. An ID
outside the embedded set is rejected. Apply cannot be restricted to a subset:

```powershell
python manage.py repair_confirmed_legacy_ticket_prices --apply
```

Apply validates and locks the complete set in one transaction. It rejects a
missing/mismatched reservation, an existing conflicting snapshot, a refunded or
ambiguous consumption, and any closed August, September, or October 2026
settlement. The three free rows must already have real zero-price purchase and
consumption evidence. A 3,500-yen row may have one active, purchase-less,
NULL-price consumption or no consumption. In the latter case only a purchase-less
legacy evidence row is created; no purchase lot is invented.

Member-name validation ignores Unicode whitespace (including half-width and
full-width spaces) but still rejects any difference in the name characters.

Only `TicketConsumption.unit_price_snapshot` (or the missing evidence row) and
`Reservation.participant_ticket_price_snapshot` are changed. Ticket balances,
ledgers, purchase totals/remnants, reservation state, lesson execution state, and
dates are untouched. Each actually changed draft month is recalculated once by
the canonical settlement service. Future October lessons remain subject to the
normal executed-lesson revenue rule, and later carry is left to the canonical
settlement chain.
