# Codex作業開始スクリプト

`start-codex.ps1` は、GitHub CLIの認証とGitの状態を確認し、cleanな `main` を `origin/main` へfast-forwardで同期してからCodexを起動します。Codexはリポジトリ内だけを書き込み可能にする `workspace-write` と、危険操作や権限外操作で確認する `on-request` を組み合わせて起動します。PC全体への無制限アクセスは許可しません。

## 起動方法

PowerShellでリポジトリのルートへ移動し、次を実行します。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-codex.ps1
```

未コミット変更がある場合、スクリプトは `main` への切り替えやpullを行わず、日本語の警告を表示して終了します。GitHub CLIの未導入、未認証、Git同期失敗、Codex CLI未導入の場合も、原因を表示して終了します。

## デスクトップショートカットの作成方法

1. デスクトップを右クリックし、［新規作成］→［ショートカット］を選びます。
2. 項目の場所へ次の形式で入力します。`<tennis-appの絶対パス>` は実際の保存場所へ置き換えてください。

```text
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<tennis-appの絶対パス>\scripts\start-codex.ps1"
```

3. 名前を「Tennis App Codex」などにして完了します。
4. 必要に応じてショートカットのプロパティを開き、「作業フォルダー」を `tennis-app` の絶対パスに設定します。

## 今後Codexへ送る最短の作業指示

```text
AGENTS.mdに従い、次の改善を実施してください。
〈改善内容〉
問題がなければコミット、push、ドラフトPR、ローカルreport.md作成まで進めてください。
```

## レビュー承認後の最短マージ指示

```text
レビュー承認。PRをマージし、mainを同期してください。
```

## 安全方針

force push、rebase、reset、ファイル削除、作業ブランチ削除、本番操作は自動化しません。これらが必要な場合はCodexが停止し、理由と必要な判断を示します。マージもレビュー承認が明示された後の別指示でのみ行います。

`report.md` と `.pr-body.md` はローカル作業用です。`.gitignore` に登録されており、コミットやPRには含めません。
