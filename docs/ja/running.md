# プロジェクトの実行

現在、このプロジェクトは単一の ESP32-CAM 系デバイスから JPEG スナップショットを受信・表示するサーバーを提供します。現行マイルストーンは **単一カメラのスナップショット upload** に絞っており、複数カメラ管理、ML ベース認識、リモート I/O 制御は後続フェーズです。

### 1. サーバーの実行:

*   ターミナルで `script` フォルダを含むディレクトリに移動します。
*   仮想環境を作成した場合は、それがアクティブになっていることを確認してください。
    ```bash
    # Windowsの場合
    # venv\Scripts\activate
    # macOS/Linuxの場合
    # source venv/bin/activate
    ```
*   Flaskサーバーを実行します:
    ```bash
    python script/server.py
    ```
*   デフォルトでは、サーバーは `http://0.0.0.0:8080/` で起動します。
*   ターミナルにサーバーが実行中であることを示す出力が表示されます (類似の形式):
    ```
     * Serving Flask app 'server'
     * Debug mode: off
     * Running on all addresses (0.0.0.0)
     * Running on http://127.0.0.1:8080
     * Running on http://<your_local_ip>:8080
    Press CTRL+C to quit
    ```
    同じネットワーク上の別のデバイス (ESP32-CAMなど) からサーバーにアクセスする場合は、`<your_local_ip>` をメモしておきます。

### 2. クライアントの設定と実行 (ESP32-CAM):

*   ESP32-CAM が以下のようにプログラムされていることを確認してください:
    *   サーバーと同じWi-Fiネットワークに接続する。
    *   JPEG スナップショットを撮影する。
    *   各スナップショットを HTTP POST リクエストとして `http://<server_ip_address>:8080/upload` に送信する。
    *   必要に応じて撮影の間に deep sleep に戻る。
*   ESP32-CAM に電源を投入して実行します。

### 3. アップロードされた画像へのアクセス:

*   画像がアップロードされたら、Webブラウザで次のURLに移動して表示できます:
    `http://<server_ip_address>:8080/files`
*   このページには、正常にアップロードされたすべての画像ファイルが一覧表示されます。ファイル名をクリックすると画像が表示されます。
*   アップロード画像は設定された upload ディレクトリ（デフォルトは `uploads/`）に保存されます。

### 4. Health Check:

*   プロセスの生存確認には以下を利用します:
    `http://<server_ip_address>:8080/healthz`

### 将来の機能強化:

後続フェーズで次を追加する可能性があります:
*   複数カメラ識別
*   保存先の切替（NAS / Google Drive）
*   短い動画クリップ
*   ML 処理
