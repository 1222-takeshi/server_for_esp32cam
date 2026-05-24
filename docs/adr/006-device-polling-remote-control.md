# ADR-006: Device polling による ESP32-CAM 遠隔制御

- **日付**: 2026-05-24
- **ステータス**: 提案

## コンテキスト

ペット監視MVPでは、ESP32-CAMの撮影間隔・解像度・向きなどをLAN内から調整し、現地でUSB接続やファームウェア再書き込みを行う頻度を減らしたい。

一方で、MVPの運用はdeep sleepを前提にしており、ESP32-CAMが常時オンラインとは限らない。睡眠中のデバイスへサーバーから即時命令を送ることはできないため、リアルタイム制御よりも、次回起床時に安全に設定を反映できる仕組みを優先する必要がある。

## 決定

MVPの遠隔制御は、サーバーにカメラ単位の desired config を保存し、ESP32-CAMが起床時または周期処理時にHTTPで取得・反映・ackする **Device polling方式** とする。

## 理由

- deep sleep中のESP32-CAMへ直接命令できない制約と整合する。
- 既存のHTTPアップロード、APIキー認証、Flask/SQLite構成を活用できる。
- WebSocket常時接続やMQTT brokerを必須にせず、Raspberry Pi / Raspberry Pi Zero上のMVP構成を単純に保てる。
- サーバー側に「望ましい設定」と「デバイスが最後に適用したversion」を分けて保持でき、未反映状態をUI/APIで確認しやすい。
- 設定値をホワイトリスト化しやすく、任意コマンド実行のような危険な制御を避けられる。

## 却下した代替案

| 代替案 | 却下した理由 |
|--------|------------|
| Server push / WebSocket command | 接続中なら低遅延だが、deep sleep中は届かない。接続維持、再接続、接続数上限、認証更新の扱いがMVPには重い |
| ESP32-CAM側HTTP server | デバイスIP探索、到達性、認証、sleep復帰の扱いが複雑。サーバーからデバイスへ直接到達できる前提を置きたくない |
| MQTT broker | IoTのdesired stateには適しているが、broker運用が増え、現行Flask/SQLite構成よりMVPの運用負荷が高い |
| 任意コマンド実行API | 実装は柔軟だが、誤操作・権限昇格・秘密情報漏洩・デバイス破損のリスクが高い |

## 結果・影響

- 設定変更は即時反映ではなく、次回起床または次回polling時に反映される。
- APIには、管理者向け desired config CRUD と、デバイス向け desired config 取得/ack エンドポイントが必要になる。
- metadata storeには、config version、更新時刻、最後にackされたversion、ack結果を保存する必要がある。
- ESP32-CAMファームウェアは、起床後に設定取得、範囲チェック、反映結果ackを行う処理を追加する必要がある。
- 将来、ライブ配信やモーター制御など低遅延制御が必要になった場合は、WebSocket command またはMQTTを別ADRで再評価する。
