# ESP32-CAM スナップショットサーバー

このプロジェクトは、ESP32-CAM 向けの **snapshot-first な中央サーバー** を提供します。現在のマイルストーンは意図的に絞っています。

* 単一の ESP32-CAM から HTTP 経由で JPEG スナップショットを受信する
* アップロードされた画像を中央サーバーに保存する
* ブラウザから保存済み画像を閲覧する
* 将来的に NAS や Google Drive のような保存先を追加できるよう、サーバー構造を小さく保つ

主な運用モデルは、**低電力な ESP32-CAM ファームウェア** が起床し、スナップショットを撮影・送信して deep sleep に戻る形です。連続動画、複数カメラの統合管理、ML 処理、リモート I/O 制御は後続フェーズとして扱います。

## 現在の機能

* **スナップショットアップロード**
    * `POST /upload` は raw JPEG bytes と `X-Device-Id` / `X-Captured-At` ヘッダを受け付けます。
    * 受信したファイルは設定されたストレージ backend に保存されます。
* **ブラウザからの閲覧**
    * `GET /` で最新画像、device metadata、主要リンクを表示します。
    * `GET /files` で保存済み画像を新しい順に一覧表示し、利用可能なら metadata も表示します。
    * `GET /files/<filename>` で個別画像を返します。
* **運用しやすさ**
    * `GET /healthz` で軽量な liveness check を提供します。
    * app factory と storage abstraction を使い、まずはローカルディスク、将来は NAS / Google Drive に差し替えやすい構成にしています。

## 今後の予定

* 同じサーバー API の裏側で保存先 backend を追加する
    * NAS
    * Google Drive
* 単一カメラ経路が安定した後に、camera-aware な一覧や保存整理を拡張する
* 連続動画ではなく、必要なら短い clip upload を後続フェーズで評価する
* snapshot と storage path が安定した後に ML パイプラインを再検討する

セットアップ、使用方法、トラブルシューティング、今後の開発に関する詳細は `docs/ja` ディレクトリを参照してください。

*   [要件](docs/ja/requirements.md)
*   [セットアップとインストール](docs/ja/setup.md)
*   [プロジェクトの実行](docs/ja/running.md)
*   [使用方法](docs/ja/usage.md)
*   [トラブルシューティング](docs/ja/troubleshooting.md)
*   [コントリビューション](docs/ja/contributing.md)
*   [将来の開発 / ロードマップ](docs/ja/roadmap.md)
*   [ライセンス](docs/ja/license.md)
*   [謝辞](docs/ja/acknowledgments.md)
