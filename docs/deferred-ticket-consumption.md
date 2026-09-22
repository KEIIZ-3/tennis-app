# 後払いチケット消費

予約時に購入ロットがなくても、残高減算と`TicketLedger`は従来どおり一度だけ記録する。同時に、`purchase = NULL`かつ`unit_price_snapshot = NULL`の`TicketConsumption`を未精算証拠として保存する。

後日の`purchase_tickets()`は購入の`+N` Ledgerを作成した後、正本サービス`allocate_new_ticket_purchase_to_deferred()`を呼ぶ。同一Userの有効・未返却・購入日時より前の未精算証拠を`TicketConsumption.created_at, Reservation.ticket_consumed_at, Reservation.id, TicketConsumption.id`順で決定的にFIFO充当する。充当は`TicketConsumption.purchase`と価格snapshotを確定し、`TicketPurchase.remaining_tickets`を割当枚数だけ減らす。顧客残高、追加Ledger、wallet、コート精算は変更しない。

キャンセルまたは返却済みの証拠は対象外。締め済み月の未精算証拠は購入lotへ自動充当せず、管理対象として残す。通常月は全消費証拠がロットに紐付いた時点で既存snapshot helperにより価格を確定する。対象月にdraft精算が存在すれば、同じ月を一度だけ`calculate_monthly_settlement(..., force=True)`で再計算し、後続draft月のcarryは既存canonical chainへ任せる。保存価格が1,000円以下ならボール代按分対象外、1,001円以上なら対象となる。

`audit_missing_ticket_purchase_evidence`は既存欠落行について、後発Purchaseの現在の未割当残数を使ったFIFO候補をREAD ONLYで表示する。監査結果だけでは修復せず、本番修復には別途明示承認が必要である。既存`repair_legacy_ticket_consumptions`は購入時点ですでに存在したロットのlegacy linkage修復に限定する。

`audit_ticket_consumptions`はこの未精算証拠をpaid/free/adjustmentへ推定せず、`classification = unverifiable`、`purchase_evidence = missing_purchase_evidence`として表示する。枚数は専用の`unverifiable_ticket_count`へ計上するが、価格証拠がないため売上金額へは算入しない。返却済みかどうかなどのライフサイクル状態は購入分類と独立して表示する。
