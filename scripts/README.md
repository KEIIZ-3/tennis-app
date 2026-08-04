# Tennis-App Codex自動化

## 概要

デスクトップの「Tennis-App 開発」をダブルクリックし、改善内容だけを1回入力すると、Codexが調査、実装、テスト、差分確認、ローカルreport.mdとhandoff.jsonの作成まで進めます。Codex正常終了後、親PowerShellがhandoff.jsonに基づいてコミット、push、Draft PR作成を行います。

レビュー承認後は「PRマージ」をダブルクリックしてPR番号だけを入力すると、PR検証、Ready化、merge commit、main同期まで進みます。

Codexはworkspace-writeで動作し、GitコマンドやGitHub CLIによる公開操作を実行しません。force push、rebase、reset、ブランチ削除、本番操作は自動化しません。

## 構成

- common.ps1: UTF-8、Git、GitHub CLI、認証、main同期、入力、prompt、PR、report関連の共通関数
- start-codex.ps1: 開発用ショートカットの入口。Codex正常終了後に公開工程を起動
- codex-auto.ps1: 改善内容入力、prompt-dev読込、一時prompt生成、Codex自動実行
- publish-from-handoff.ps1: handoff検証、専用ブランチ作成、対象ファイル限定のcommit、push、Draft PR作成
- merge-pr.ps1: PR番号入力後の検証、Ready、Merge、main同期
- install-shortcuts.ps1: デスクトップショートカット作成
- prompts/prompt-dev.txt: 通常開発
- prompts/prompt-review.txt: レビュー
- prompts/prompt-merge.txt: マージ方針
- prompts/prompt-hotfix.txt: 緊急修正
- prompts/prompt-refactor.txt: 挙動維持リファクタリング

new-task.ps1は廃止しました。通常開発の入口はstart-codex.ps1へ一本化しています。

## セットアップ

前提:

- Windows PowerShell
- Git
- GitHub CLI
- gh認証済み
- Codex CLI

初回のみリポジトリルートで次を実行します。

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-shortcuts.ps1

デスクトップへ次の2つが作成されます。

- Tennis-App 開発
- PRマージ

この導入操作だけがデスクトップへ書き込みます。

## 開発の使い方

1. 「Tennis-App 開発」をダブルクリックします。
2. 「改善内容を入力してください」と表示された複数行画面へ、改善要求全体を貼り付けます。
3. 「開始」を押します。
4. Draft PR URLとローカルreport.mdが作成されるまで待ちます。

長い運用promptを貼り付ける必要はありません。要件、ログ、コード、箇条書き、スクリーンショットの説明を1回で入力できます。

PowerShellから直接実行する場合:

    .\scripts\start-codex.ps1

文字列を引数で渡す場合:

    .\scripts\start-codex.ps1 -Request "改善内容"

## 内部フロー

start-codex.ps1はcodex-auto.ps1を起動します。Codexが正常終了しhandoff.jsonが存在する場合だけ、start-codex.ps1がpublish-from-handoff.ps1を親PowerShellとして続けて起動します。

1. UTF-8設定
2. GitHub CLI PATH確認
3. gh認証確認
4. Git状態確認
5. mainとorigin/mainのfast-forward同期
6. 改善内容の1回入力
7. prompts/prompt-dev.txt読込
8. {{IMPROVEMENT}}への改善内容埋込
9. ignore済み.codex-prompt.tmpの一時生成
10. Codexへpromptをstdin送信
11. Codexが調査、編集、テスト、差分確認、report.md、handoff.json作成を実施
12. Codex終了後に一時promptを削除
13. 正常終了時だけhandoff.jsonを検証
14. 親PowerShellが専用ブランチ作成、許可ファイルだけのステージ、commit、push、Draft PR作成を実施
15. report.mdのPR情報を更新し、一時的なhandoff.jsonと.pr-body.mdを削除

## PRマージ

レビュー承認後:

1. 「PRマージ」をダブルクリックします。
2. PR番号だけを入力します。
3. 完了まで待ちます。

スクリプトは次を確認します。

- PRがOPEN
- mergeableがMERGEABLE
- merge stateがCLEAN
- 変更要求がない
- ステータスチェックが成功済み、neutral、skipped、またはチェックなし
- 未解決レビューコメントがない

確認後、DraftをReadyへ変更し、merge commit方式でマージし、ローカルmainをfast-forward同期します。作業ブランチは削除しません。

直接実行する場合:

    .\scripts\merge-pr.ps1

番号を引数で渡す場合:

    .\scripts\merge-pr.ps1 -PrNumber 155

## ローカル専用ファイル

次は.gitignoreで除外され、Git管理されません。

- report.md
- .pr-body.md
- .codex-prompt.tmp

handoff.jsonもコミット対象外ですが、公開工程への受け渡し後にpublish-from-handoff.ps1が削除します。

## トラブル対応

- 未コミット変更: コミットまたは手動整理してから再実行してください。スクリプトはresetや削除を行いません。
- gh認証エラー: gh auth login後にgh auth statusを確認してください。
- main同期エラー: 履歴が分岐している可能性があります。スクリプトはrebaseやresetを行いません。
- Codex未検出: codex --versionが成功するようPATHを設定してください。
- promptテンプレートエラー: prompts/prompt-dev.txtと{{IMPROVEMENT}}を確認してください。
- PR検証エラー: GitHubでコンフリクト、checks、変更要求、未解決レビューを解消してください。
- 一時promptが残った: Codex強制終了時に残る場合があります。.codex-prompt.tmpはignore済みで、内容確認後に手動削除できます。
