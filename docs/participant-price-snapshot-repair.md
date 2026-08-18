# 参加者チケット価格snapshot補完

`Reservation.participant_ticket_price_snapshot`はmigration 0051で、既存の
`TicketConsumption`より後にnullable列として追加された。migrationには既存行の
data migrationがないため、保存済みConsumption価格があってもReservation側が
NULLの履歴が存在する。

通常の`Reservation.consume_tickets()`と後払い割当サービスは、全消費枚数について
Purchase由来の価格が確定した同一transaction内でsnapshot helperを呼ぶ。helperは
非返金、価格既知、Consumption合計枚数一致の場合だけ総額を保存する。現在価格から
推定しない。

既存行は次のコマンドで確認する。既定はdry-runでありDBを更新しない。

```powershell
python manage.py repair_participant_price_snapshots
python manage.py repair_participant_price_snapshots --reservation-id 1510
```

出力の`candidate=true`と証拠を確認後、明示的に適用する。

```powershell
python manage.py repair_participant_price_snapshots --apply
```

補完対象は、snapshotがNULL、使用枚数が正、全Consumptionが非返金、枚数が完全一致、
PurchaseとConsumptionの保存価格が一致し、単一の正の価格に確定でき、免除等の特別
ルールと競合しない予約だけである。0円、mixed、価格不明、不足、返金済みは除外する。
更新列は`Reservation.participant_ticket_price_snapshot`だけで、残高、Ledger、Purchase、
Consumption、wallet、court settlementは更新前後のinvariantで保護する。
