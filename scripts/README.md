# Tennis-App Codex自動化

## 概要

改善内容を1回入力するだけで、Codexが調査、実装、テスト、差分確認、コミット、push、Draft PR、ローカルreport.mdまで進めます。レビュー後は「PRマージ」ショートカットから、PR検証、Ready化、merge commit、main同期を実行します。

Codexはworkspace-writeで起動し、リポジトリ外やPC全体への書き込み権限を与えません。force push、rebase、reset、ブランチ削除、本番操作は自動化しません。

## ファイル構成

- codex-common.ps1: UTF-8、CLI検出、GitHub認証、Git同期、入力画面などの共通処理
- start-codex.ps1: 同期後に通常の対話型Codexを起動
- codex-auto.ps1: 入力とテンプレートを合成し、Codexを非対話実行
- new-task.ps1: 通常改善のワンクリック入口
- merge-pr.ps1: レビュー後の唯一のマージ入口
- install-shortcuts.ps1: デスクトップショートカットを作成
- prompt-base.txt: 全作業共通の自律実行・安全ルール
- prompt-dev.txt: 通常改善
- prompt-hotfix.txt: 緊急修正
- prompt-refactor.txt: 挙動維持リファクタリング
- prompt-review.txt: レビュー
- prompt-merge.txt: マージ方針

## 初回導入

PowerShellでリポジトリルートへ移動し、一度だけ次を実行します。この操作だけがデスクトップへ書き込みます。

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\\scripts\\install-shortcuts.ps1

次の2つがデスクトップに作成されます。

- Tennis-App 開発
- PRマージ

## 開発作業

1. 「Tennis-App 開発」をダブルクリックします。
2. 「改善内容を入力してください」と表示された複数行入力画面へ、要件、ログ、コード、スクリーンショットの説明などをまとめて貼り付けます。
3. 「開始」を押します。
4. CodexがDraft PRとローカルreport.mdの作成まで進めます。

コマンドから直接実行する場合:

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\\scripts\\new-task.ps1

用途を指定する場合:

    .\\scripts\\codex-auto.ps1 -TaskType hotfix
    .\\scripts\\codex-auto.ps1 -TaskType refactor
    .\\scripts\\codex-auto.ps1 -TaskType review

## PRマージ

レビュー承認後、Codexが作業を終えたブランチのまま「PRマージ」をダブルクリックします。スクリプトは次を自動確認します。

- PRがOPEN
- mergeableがMERGEABLE
- merge stateがCLEAN
- 変更要求がない
- ステータスチェックに失敗・実行中がない
- 未解決レビューコメントがない

確認後、DraftをReadyへ変更し、merge commit方式でマージし、ローカルmainをfast-forward同期します。ブランチは削除しません。

別ブランチから番号指定で実行する場合:

    .\\scripts\\merge-pr.ps1 -PrNumber 154

## 通常の対話型Codex

    .\\scripts\\start-codex.ps1

## トラブル対応

- 未コミット変更: 変更をコミット、退避、または不要なら手動で整理してから再実行してください。スクリプトはresetや削除を行いません。
- GitHub認証エラー: gh auth login後にgh auth statusを確認してください。
- main同期エラー: ローカルとリモートの履歴が分岐しています。スクリプトはrebaseやresetを行わないため、Git履歴を確認してください。
- Codex CLI未検出: codex --versionが成功するようPATHを設定してください。
- PRを特定できない: 作業ブランチでmerge-pr.ps1を実行するか、-PrNumberを指定してください。
- 未解決レビューまたはチェック失敗: GitHubで解消してから再実行してください。

report.mdと.pr-body.mdはローカル専用で、.gitignoreによりGit管理されません。
