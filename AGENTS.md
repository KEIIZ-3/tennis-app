# Tennis-App Codex作業ルール

## 1. 適用範囲

- すべてのパスはtennis-app起点で扱う。
- このファイルを作業開始時に最後まで読む。
- ユーザー入力は、長文、ログ、コード、箇条書き、スクリーンショット説明を含め、最後まで1つの改善要求として扱う。
- 現物コード、ログ、既存テスト、Git履歴に基づいて判断する。
- 不足情報があっても先に調査を進め、本当に実装不能な場合だけ1回質問する。

## 2. 実装原則

- 既存ファイル名と業務仕様を依頼なく変更しない。
- unrelatedな変更を混ぜない。
- モンキーパッチ、import副作用による差し替え、暫定回避を新規導入しない。
- リファクタリングでは計算式、辞書キー、DB保存項目、update_fields、公開インターフェース、画面仕様を維持する。
- マイグレーションは必要な場合だけ作成する。
- 必要なテストを追加または修正する。

## 3. Git運用

- 作業前にmainとorigin/mainをfast-forward同期する。
- main上で編集しない。
- 作業ごとにagent/で始まる英小文字kebab-caseの専用ブランチを作る。
- 対象ファイルだけを明示的にステージする。
- report.md、.pr-body.md、.codex-prompt.tmpをコミットしない。
- 問題がなければcommit、push、Draft PR、ローカルreport.mdまで途中確認なしで完了する。
- .pr-body.mdはDraft PR作成後に削除する。
- Draft PR作成後は停止する。

## 4. 必須確認

- git diff
- git diff --check
- git diff --stat
- 変更ファイル一覧
- 利用可能な関連テスト
- PowerShell変更時は変更した全ps1のPowerShell AST解析
- Python変更時は対象ファイルのpython -m py_compile
- report.md、.pr-body.md、.codex-prompt.tmpのignore確認
- 既存業務コードへの影響確認

実行不能な確認は、コマンド、エラー、理由をreport.mdへ記録する。

## 5. 自動化ルール

- 通常開発の入口はscripts/start-codex.ps1とする。
- start-codex.ps1はscripts/codex-auto.ps1だけを起動する。
- 共通処理はscripts/common.ps1へ集約し、入口へ重複実装しない。
- 改善内容の入力は1回だけとし、「改善内容を入力してください」と表示する。
- scripts/prompts/prompt-dev.txtの{{IMPROVEMENT}}へ入力全体を埋め込み、Codexへ自動送信する。
- 長い運用promptをユーザーへ要求しない。
- PRマージはレビュー承認後にscripts/merge-pr.ps1からのみ実行する。

## 6. 禁止事項

- force push
- rebase
- reset
- 作業ブランチ削除
- scripts/merge-pr.ps1以外からのPRマージ
- 本番操作
- 無制限なPC全体またはリポジトリ外への書き込み

禁止操作が必要な場合だけ停止して確認する。認証・権限エラー、テスト失敗、業務仕様判断が必要な場合も停止して状況を報告する。

## 7. report.md必須項目

- 変更概要
- 変更ファイル
- 設計意図
- 挙動変更の有無
- 自動化される内容
- 操作手順
- 従来との差分
- 追加・変更・廃止したスクリプト
- テンプレート一覧
- 自動化しない危険操作
- 実行した確認
- 未実行確認と理由
- PR番号
- PR URL
- 最新コミットSHA
- 残リスク
