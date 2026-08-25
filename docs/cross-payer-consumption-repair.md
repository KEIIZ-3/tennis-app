# Formal cross-payer consumption repair

`repair_cross_payer_consumptions` repairs pre-existing active `TicketConsumption`
rows whose payer was formally changed but whose purchase and price are still null.
The command is preview-only unless `--apply` is supplied.

It requires one active unpriced consumption, a matching latest
`TicketBurdenChange`, an open accounting month, and enough capacity in the
payer's oldest available non-reversed purchase. Apply updates only the existing
consumption's purchase/price, the purchase remaining capacity, and the
reservation participant price. It does not recreate consumption, change ticket
balances or ledgers, or alter burden history. A priced consumption is a noop.

```powershell
python manage.py repair_cross_payer_consumptions --reservation-id 1534
python manage.py repair_cross_payer_consumptions --reservation-id 1534 --apply
```
