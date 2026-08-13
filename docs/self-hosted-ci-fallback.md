# GitHub-hosted CI 課金制限時の self-hosted fallback

## 目的

通常はGitHub-hosted runnerで既存の`CI` workflowを実行し、GitHub Actionsの支払い失敗またはspending limitでjobが開始できない場合だけ、登録済みのself-hosted runnerで同じ検証を実行します。

コード起因のテスト失敗、キャンセル、runner pool障害、未知のエラーはfallback対象にしません。判定できない場合はfail-closedします。

## 構成

`.github/workflows/ci-fallback.yml`は`CI` workflowの`workflow_run`完了イベントで起動します。

| Runner label | 用途 | 権限 |
|---|---|---|
| `esp32cam-ci-gate` | 完了済みCIの判定とCheck Run更新 | `actions: read`、`checks: write`、`contents: read` |
| `esp32cam-ci-fallback` | 信頼済みcommitのlint/test/audit/ESP32 compile | `contents: read`のみ |

gateとcandidate runnerは分離します。candidate jobにはCheck Run更新権限、GitHub tokenの明示的な環境変数、Docker socket、host workspaceを渡しません。

## fallback判定

次の条件をすべて満たす失敗jobだけをbilling failureと判定します。

- `status=completed`かつ`conclusion=failure`
- `steps=[]`
- `runner_id=0`
- failure annotationに、account payment failureとspending limit increaseの両方の意味要素が含まれる

期待job（`test (3.11)`、`test (3.12)`、`dependency-audit`、`compile-esp32-example`）の欠落・重複もfallbackせず失敗扱いにします。

## 初期セットアップ

前提:

- Docker daemonが起動している
- `gh auth status`が成功し、対象repositoryのActions runner管理権限がある
- このPRがdefault branchへマージ済み
- self-hosted runnerを置くホストを信頼できる

```bash
scripts/github-runner.sh build
scripts/github-runner.sh install all
scripts/github-runner.sh status all
```

`install`はGitHub APIから短時間有効なregistration tokenを取得し、runner containerの名前付きpipeへ一度だけ渡します。tokenはimage、repository file、永続環境変数へ保存しません。

runner containerは次の制約で起動します。

- `--cap-drop=ALL`
- `--security-opt=no-new-privileges`
- 非rootの`runner`ユーザー
- Docker socket、host workspace、host homeをmountしない
- runner配布物のversion固定とSHA-256検証
- `--disableupdate`で実行中のrunner自己更新を無効化

## 日常運用

```bash
# GitHub登録状態とcontainer状態
scripts/github-runner.sh status all

# ログ確認
scripts/github-runner.sh logs all

# 一時停止・再開
scripts/github-runner.sh stop all
scripts/github-runner.sh start all
```

runnerがofflineの場合、workflow内のfallback jobは開始できません。Docker daemon、container、GitHub疎通を確認してください。

## required check設定

branch protectionでは、既存CIのjobに加えて`CI Fallback Gate`をrequired checkへ追加する必要があります。GitHub-hosted CIが成功した場合もgateはsuccessを報告し、billing failure時はself-hosted workloadが全成功した場合だけsuccessを報告します。

private repositoryのGitHubプランや設定によりcustom Check Runをrequired checkへ指定できない場合は、マージ前に人間が次を確認してください。

- GitHub-hosted `CI`が成功、または厳格なbilling failure
- billing failureの場合、`CI Fallback Gate`がsuccess
- LunaレビューでBLOCKERなし
- `act`またはself-hosted runnerでの検証証跡がある

## セキュリティ上の注意

- fork由来の`workflow_run`は拒否します。
- repository owner以外が起動したrunは拒否します。
- fallback candidateは`contents: read`だけを持ち、Check Runを書き換えられません。
- dependency installとESP32 toolchain取得は外部ネットワークへ接続します。self-hostedホスト自体を信頼できるネットワークに置いてください。
- self-hosted runnerはホストkernelとDocker daemonを信頼します。Docker containerはVM境界ではありません。より広いcontributorを受け入れる場合はjobごとのephemeral runnerまたは専用VMへ移行してください。

## 解除・ロールバック

一時停止:

```bash
scripts/github-runner.sh stop all
```

完全解除:

```bash
scripts/github-runner.sh unregister all
```

コードを戻す場合は`.github/workflows/ci-fallback.yml`、`scripts/ci/`のfallback補助コード、`scripts/github-runner.sh`、`infra/github-actions-runner/`をrevertします。既存のGitHub-hosted `CI` workflow自体は維持されます。
