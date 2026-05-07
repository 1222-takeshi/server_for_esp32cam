# ESP32-CAM スナップショットサーバー

このプロジェクトは、ESP32-CAM 向けの **snapshot-first な中央サーバー** を提供します。現在のマイルストーンは次まで広がっています。

* 複数の ESP32-CAM から HTTP 経由で JPEG スナップショットを受信する
* アップロードされた画像を中央サーバーに保存する
* カメラごとの状態と保存済み画像をブラウザから閲覧する
* upload 後にサーバー側で人検知を非同期実行する
* 将来的に NAS や Google Drive のような保存先を追加できるよう、サーバー構造を小さく保つ
* `examples/` に最小 ESP32-CAM クライアント例を置き、upload 契約を end-to-end で確認できるようにする

主な運用モデルは、**低電力な ESP32-CAM ファームウェア** が起床し、スナップショットを撮影・送信して deep sleep に戻る形です。連続動画とリモート I/O 制御は後続フェーズとして扱います。

## 現在の機能

* **スナップショットアップロード**
    * `POST /upload` は raw JPEG bytes と `X-Device-Id` / `X-Captured-At` ヘッダを受け付けます。
    * 受信したファイルは camera-aware なパスへ保存され、SQLite metadata にも登録されます。
* **複数台監視**
    * `GET /` はカメラごとの最新状態、最新 snapshot、検知結果を表示します。
    * `GET /cameras/<camera_id>` で特定カメラの snapshot と人検知履歴を確認できます。
    * `/api/` 配下に camera / snapshot / detection の JSON API があります。
* **人検知**
    * 各 snapshot は upload 後に非同期で person-only 推論へ送られます。
    * 設計基準は「local PC で開発しつつ、将来 Raspberry Pi 4 / CPU-only へ移せること」です。
    * 実際の推論には `ml` extra と TFLite モデル設定が必要です。
* **クライアント例**
    * `examples/` にある最小スケッチは、1 回撮影して `/upload` へ送信し、必要なら deep sleep に戻る流れを示します。開発中は OTA 更新も可能です。
    * まずはこの例を使うと、必要なヘッダ、payload 形式、サーバー URL を実機で合わせやすくなります。
* **ブラウザからの閲覧**
    * `GET /files` で保存済み画像を新しい順に一覧表示し、利用可能なら metadata も表示します。
    * `GET /files/<filename>` で個別画像を返します。
* **運用しやすさ**
    * `GET /healthz` で軽量な liveness check を提供します。
    * app factory と storage abstraction を使い、まずはローカルディスク、将来は NAS / Google Drive に差し替えやすい構成にしています。

## 今後の予定

* 同じサーバー API の裏側で保存先 backend を追加する
    * NAS
    * Google Drive
* Raspberry Pi 4 / CPU-only 前提で人検知パイプラインをさらに調整する
* ダッシュボードと検知履歴が安定した後に通知を追加する
* 連続動画ではなく、必要なら短い clip upload を後続フェーズで評価する

セットアップ、使用方法、トラブルシューティング、今後の開発に関する詳細は `docs/ja` ディレクトリを参照してください。

*   [要件](docs/ja/requirements.md)
*   [セットアップとインストール](docs/ja/setup.md) - macOSでの `.venv` 作成、依存関係インストール、ESP32-CAM接続時の前提を含みます。
*   [プロジェクトの実行](docs/ja/running.md) - macOSのLAN IP確認と実行手順を含みます。
*   [使用方法](docs/ja/usage.md)
*   [トラブルシューティング](docs/ja/troubleshooting.md)
*   [コントリビューション](docs/ja/contributing.md)
*   [将来の開発 / ロードマップ](docs/ja/roadmap.md)
*   [ライセンス](docs/ja/license.md)
*   [謝辞](docs/ja/acknowledgments.md)
