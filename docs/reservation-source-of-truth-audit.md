# Reservation Source of Truth 最終監査

## Phase 2 READ ONLYデータ診断

静的監査に加えて、occurrence単位の本番データ診断は次のコマンドで実行する。

```powershell
python manage.py diagnose_reservation_integrity
```

PostgreSQLではコマンド自身が `SET TRANSACTION READ ONLY` を設定する。診断は
`SELECT` のみを使用し、`save`、`update`、`get_or_create`、`select_for_update`、repairを
実行しない。結果はJSONで、canonical key（`fixed:<id>:<date>`、
`availability:<id>`、関係IDのない旧データは `legacy:...`）ごとの人数・定員・実施状態と、
severity付きfindingを出力する。`FixedLesson.members` は未来Reservation欠落検出にだけ使い、
開催回人数には加算しない。`LessonWaitlist` も参加人数には含めない。

## 結論

個別開催回の参加者一覧、参加人数、満員判定、参加者向け通知対象は、有効な
`Reservation` (`status=active`) を唯一の正本とする。全状態を表示する実施管理でも、
開催回の識別は同じ共通サービスを使い、`fixed_lesson`、`availability`、物理枠の順に
安定した関係IDを優先する。

最終監査で、実施管理だけが固定開催回の `fixed_lesson` と `availability` をOR結合し、
同じavailabilityを共有する別レッスンの予約を混入させ得る経路を確認した。
`reservations_for_lesson` へ統一し、固定レッスンIDを優先するよう修正した。

## 監査したファイル

- 正本・同期: `club/models.py`, `club/reservation_service.py`, `club/lesson_participants.py`, `club/fixed_occurrence_participants.py`, `club/fixed_lesson_membership_service.py`, `club/fixed_lesson_integrity_service.py`, `club/fixed_lesson_sync_facade.py`, `club/signals.py`
- 画面・管理・受付: `club/views.py`, `club/admin.py`, `club/admin_dashboard.py`, `club/analytics_dashboard.py`, `club/lesson_member_list.py`, `club/coach_fixed_lesson_weekly.py`, `club/lesson_execution.py`, `club/templates/reservations/*.html`, `club/templates/coach/*.html`, `club/templates/admin/reservations_manage.html`
- 通知: `club/court_number_line_notice.py`, `club/notifications.py`, `club/services/notifications.py`, `club/tasks.py`
- 精算・給与: `club/settlement_loader.py`, `club/settlement_service.py`, `club/settlement_balance_policy.py`, `club/settlement_calculator.py`, `club/settlement_coach_calculation.py`, `club/settlement_views.py`, `club/settlement_integrity_diagnostic.py`, `club/today_lesson_actions.py`
- 回帰テスト: `club/test_reservation_flow.py`, `club/tests/test_fixed_lesson_occurrence_cancel_calendar.py`, `club/tests/test_fixed_occurrence_calendar_count.py`, `club/tests/test_settlement_unconfirmed_execution.py`, `club/tests/test_settlement_wallet_policy.py`

## Reservation参照箇所

- `lesson_participants` / `fixed_occurrence_participants`: 開催回参加者の共通取得とカレンダー人数。
- `models.Reservation`: 有効化時の満員判定、同一開催回人数、重複検証。
- `views` / `lesson_member_list` / `coach_fixed_lesson_weekly`: 予約確認、管理画面、受付画面、本日の参加人数。
- `court_number_line_notice` / `notifications` / `signals`: LINE・メールの参加者宛先と予約単位通知。
- `lesson_execution`: 実施管理の全状態予約。固定開催回は共通取得サービスで関係IDを優先する。
- `admin_dashboard` / `analytics_dashboard`: 当日・期間参加実績。
- `settlement_loader` / `settlement_balance_policy` / `settlement_service`: 月次精算、実施済み参加人数、給与計算の入力。
- `admin`: 将来予約数と予約自体の管理表示。

## FixedLesson参照箇所

- `FixedLesson.members`: 将来の `Reservation` を生成・同期する定期参加設定（C: 意図的に別正本）。
- `FixedLesson.capacity` とコーチ・コート・曜日・日時: 開催設定と定員設定（C）。
- カレンダー、予約確認、受付、実施管理では開催回の識別子としてのみ使用し、参加者はReservationから取得（A）。
- 管理画面の「固定参加メンバー」「固定メンバー数」は定期設定の管理表示であり、開催回参加人数ではない（C）。

## LessonWaitlist参照箇所

- 未予約者の待機列、待機人数、繰り上げ候補、空席LINE通知（C: 意図的に別正本）。
- `LessonWaitlistParticipant` は待機者の家族参加者スナップショット（C）。
- 正式参加者数、満員判定、精算人数、給与人数には使用しない（A）。

## 分類結果

### A 問題なし・変更不要

- LINE通知、メール通知、予約確認、カレンダー、管理画面参加者一覧、受付画面、本日の精算、月次精算、給与計算はReservationを参加実績として参照する。
- 満員判定は対象開催回の有効Reservation件数と定員設定を比較する。
- `ReservationParticipant` はReservationに従属する表示スナップショットで、参加枠を増やさない。
- 会員数、低チケット会員数、アンケート回答数、コーチ人数はレッスン参加人数ではないため各対象モデルを集計する。

### B Reservationへ統一した箇所

- `club/lesson_execution.py`: 固定開催回の `fixed_lesson` と `availability` のOR検索を廃止し、`reservations_for_lesson` に統一した。実施管理に必要なactive/pending/canceled/rain_canceledの全状態表示は維持する。

### C 意図的に別正本

- `FixedLesson.members`: 将来予約生成用の定期設定。
- `LessonWaitlist` / `LessonWaitlistParticipant`: 予約確定前の待機列と参加者スナップショット。
- `CoachAvailability.capacity` / `FixedLesson.capacity`: 定員設定。
- `ReservationParticipant`: Reservationに従属する参加者表示スナップショット。

## 回帰防止

同じavailability、コーチ、コート、日時、レッスン種別を持ちながら別のFixedLessonへ
紐づくReservationを作り、実施管理の対象へ混入しないテストを追加した。今後、開催回の
参加者や人数を取得するコードを追加するときは `reservations_for_lesson` または
`reservations_for_object` を使用し、`FixedLesson.members.count()`、物理枠だけの独自検索、
テンプレート内の独自集計を追加しないこと。待機人数は参加人数と明確に別名で扱うこと。
