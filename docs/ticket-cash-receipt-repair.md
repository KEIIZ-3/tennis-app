# チケット現金受領の既存データ補修

`TicketCashReceipt` はチケット権利ロットとは独立した現金受領の正本です。既存購入の受領日は推定せず、確認済みの事実だけをCSVで投入します。

CSVはUTF-8で、次のヘッダーを使用します。

```csv
ticket_purchase_id,amount,received_at,payment_method
<確認済み購入ID>,<確認済み金額>,2026-08-02T12:00:00+09:00,cash
```

最初にpreviewを実行します。previewはDBを変更しません。

```powershell
python manage.py repair_ticket_cash_receipts .\receipts.csv --created-by <処理者ユーザーID>
```

出力内容と対象月が正しいことを確認後、同じCSVを明示的に適用します。

```powershell
python manage.py repair_ticket_cash_receipts .\receipts.csv --created-by <処理者ユーザーID> --apply
```

適用は購入ID単位の冪等キーを保存します。再適用、0円、未知の支払方法、不正な日時、存在しない購入、締め済み受領月はエラーになり、全件をロールバックします。受領が未確認の購入はCSVへ含めないでください。
