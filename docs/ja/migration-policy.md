# DB スキーマ変更とマイグレーション方針

このドキュメントでは、SQLite データベーススキーマの変更手順と互換性ポリシーを説明します。

---

## 自動マイグレーションの仕組み

サーバーは起動時に `script/metadata.py` の `_migrate()` 関数を実行し、DB スキーマを最新版に自動更新します。

手動マイグレーションコマンドは不要です。`docker compose up -d --build` するだけで適用されます。

### マイグレーションの実装パターン

```python
# script/metadata.py の _migrate() 内
def _migrate(conn):
    # 例: 新しいカラムを既存テーブルに追加する
    _add_column_if_missing(conn, "cameras", "display_name", "TEXT")
    _add_column_if_missing(conn, "cameras", "expected_interval_seconds", "INTEGER")
```

`_add_column_if_missing()` は冪等です。すでにカラムが存在する場合は何もしません。

---

## スキーマ変更ルール

### 後方互換性のある変更（推奨）

以下の変更は既存データを壊さないため、安全に行えます:

| 変更の種類 | 注意事項 |
|----------|---------|
| カラムを追加する（nullable または DEFAULT 付き） | `NOT NULL` + DEFAULT なしは既存行への挿入で失敗するため不可 |
| インデックスを追加する | パフォーマンス向上のみ |
| テーブルを追加する | 既存テーブル・データに影響しない |
| VIEW や TRIGGER を追加する | 同上 |

### 後方互換性のない変更（要注意）

以下の変更は既存データを壊す可能性があります:

| 変更の種類 | 対応手順 |
|----------|---------|
| カラムを削除する | バックアップ後にマイグレーションを実施 |
| カラムの型を変更する | 新カラムを追加してデータをコピーし、旧カラムを削除 |
| テーブルを削除・名前変更する | 同上 |
| 既存行を変更する | バックアップとロールバック手順を準備してから実施 |

---

## アップグレード手順

### 標準アップグレード（スキーマ変更なし、または後方互換のある変更）

```bash
git pull
docker compose up -d --build
curl http://localhost:8080/healthz/ready
```

### 後方互換性のない変更を含むアップグレード

```bash
# 1. バックアップを取得する（docs/ja/operations-runbook.md を参照）
BACKUP_DIR="./backup/pre-migration-$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"
docker compose stop app
docker run --rm -v server_for_esp32cam_data:/src -v "$(pwd)/$BACKUP_DIR":/dst \
  busybox cp /src/esp32cam.sqlite3 /dst/esp32cam.sqlite3

# 2. アップグレードを実施する
git pull
docker compose up -d --build

# 3. ヘルスチェックで確認する
curl http://localhost:8080/healthz/ready
```

---

## ロールバック

スキーマ変更を含むアップグレードのロールバックは、バックアップからのリストアで行います。

> スキーマ変更は一般に不可逆です（カラム追加後に戻すと既存データが失われる可能性があります）。
> ロールバックが必要な場合は必ずバックアップからリストアしてください。

リストア手順: [`docs/ja/operations-runbook.md`](operations-runbook.md) のセクション 7 を参照。

---

## 検証観点

アップグレード後の確認リスト:

- [ ] `GET /healthz/ready` が `"status": "ready"` を返す
- [ ] `GET /api/cameras` がカメラ一覧を返す
- [ ] `POST /upload` が JPEG を正常に受け付ける
- [ ] 既存スナップショットが `GET /api/cameras/{id}/snapshots` で閲覧できる

---

## 関連ドキュメント

| ドキュメント | 内容 |
|------------|------|
| [`docs/ja/operations-runbook.md`](operations-runbook.md) | バックアップ・リストア手順 |
| [`docs/adr/002-sqlite-storage.md`](../adr/002-sqlite-storage.md) | SQLite 採用の理由 |
