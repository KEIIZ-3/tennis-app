# Reservation Source of Truth 監査

## 方針

個別開催回の参加者一覧、参加人数、満員判定、参加者向け通知対象は、有効な
`Reservation` (`status=active`) を唯一の正本とする。固定レッスンへの明示的な
紐付けがある場合は、コーチ・コート・日時が一致するだけの別予約を混ぜない。

## ① 問題なし

- `club/lesson_participants.py` と `club/fixed_occurrence_participants.py`: 開催回の有効予約を共通取得する。
- `club/lesson_member_list.py`: コーチ画面の参加者一覧と人数を有効予約から生成する。
- `club/coach_fixed_lesson_weekly.py`: 固定レッスン週間画面の氏名と人数を有効予約から生成する。
- `club/court_number_line_notice.py`: LINE対象とメール補完対象を開催回の有効予約から取得する。
- `club/admin_dashboard.py`、`club/analytics_dashboard.py`: 参加者実績を有効予約から集計する。
- `club/lesson_execution.py` と精算関連モジュール: 実施・売上・精算対象を予約行から取得し、用途に応じて予約状態を絞る。
- 予約一覧・予約詳細・管理用予約一覧: 表示対象自体が `Reservation` であり、キャンセル状態も予約行の状態として表示する。
- `Reservation._validate_capacity_before_activation`: 有効予約だけで満員判定する。

## ② Reservation へ統一した箇所

- 固定レッスンのカレンダー人数: 物理枠の集計値との `max` を廃止し、対象
  `FixedLesson` に明示的に紐づく有効予約だけを数えるよう変更した。
- 固定レッスンと通常枠の予約確認: コーチ・コート・日時による独自集計を廃止し、
  `reservations_for_lesson` を使用するよう変更した。固定レッスンまたは開催枠の
  関係IDを優先するため、同一物理枠の別レッスンを混在させない。
- カレンダーの import 副作用差し替え: 本体と重複していた
  `lesson_calendar_fixes.py` を廃止した。人数表示と満員判定は正規のビューと
  `Reservation` モデルの検証経路で完結する。

## ③ 設計上意図的なので変更不要

- `FixedLesson.members`: 個別開催回の参加実績ではなく、将来の `Reservation`
  を生成・同期するための定期参加設定。管理画面の「固定メンバー」設定数、整合性監査、
  メンバー追加・解除処理に限って使用する。
- `LessonWaitlist` / `LessonWaitlistParticipant`: 未予約者のキャンセル待ちと参加者
  スナップショット。空席通知前は `Reservation` が存在しないため独立した正本とする。
- `ReservationParticipant`: 予約に従属する家族参加者の表示スナップショット。
  参加枠そのものは `Reservation` であり、人数を増減させる別ソースではない。
- `CoachAvailability.capacity` / `FixedLesson.capacity`: 参加者情報ではなく定員設定。
- `User` の会員数、低チケット会員数、アンケート回答者数: レッスン参加人数ではない
  業務指標のため、対象モデルから集計する。
- `LessonWaitlist` の待機人数: 参加人数ではなく待機列の件数として別表示する。

## 回帰防止

同じコーチ・コート・日時・レッスン種別を持つ無関係な予約を作成し、固定レッスンの
カレンダーと予約確認の人数へ混入しないテストを追加した。キャンセル済み予約を数えない
既存テスト、コーチ画面・通知対象の既存テストと合わせて境界を保証する。
