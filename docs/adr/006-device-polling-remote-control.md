# ADR-006: Device polling による ESP32-CAM 遠隔制御

- **日付**: 2026-05-24
- **ステータス**: 提案

## コンテキスト

ペット監視MVPでは、ESP32-CAMの撮影間隔・解像度・向きなどをLAN内から調整し、現地でUSB接続やファームウェア再書き込みを行う頻度を減らしたい。また、既存のArduinoOTA/PlatformIO OTA upload手順を活用し、現地でUSB接続しなくてもファームウェア更新できる状態を維持したい。

一方で、MVPの運用はdeep sleepを前提にしており、ESP32-CAMが常時オンラインとは限らない。睡眠中のデバイスへサーバーから即時命令を送ることはできないため、リアルタイム制御よりも、次回起床時に安全に設定を反映できる仕組みを優先する必要がある。

## 決定

MVPの遠隔制御は、サーバーにカメラ単位の desired config を保存し、ESP32-CAMが起床時または周期処理時にHTTPで取得・反映・ackする **Device polling方式** とする。

OTA更新についても同じ考え方を適用し、deep sleep中に直接書き込むのではなく、desired configで「次回起床時にOTA待受ウィンドウを開く」要求を出す。デバイスは起床後にWi-Fi接続し、非空のOTAパスワードが設定されている場合のみ、指定秒数だけArduinoOTA等のOTAサービスを処理してから、成功・失敗・timeoutをackし通常の撮影・アップロード・deep sleepへ戻る。

## 理由

- deep sleep中のESP32-CAMへ直接命令できない制約と整合する。
- 既存のHTTPアップロード、APIキー認証、Flask/SQLite構成を活用できる。
- WebSocket常時接続やMQTT brokerを必須にせず、Raspberry Pi / Raspberry Pi Zero上のMVP構成を単純に保てる。
- サーバー側に「望ましい設定」と「デバイスが最後に適用したversion」を分けて保持でき、未反映状態をUI/APIで確認しやすい。
- 設定値をホワイトリスト化しやすく、任意コマンド実行のような危険な制御を避けられる。
- OTA更新も起床中の明示的な待受ウィンドウに限定でき、常時起床による消費電力増加を避けられる。

## 却下した代替案

| 代替案 | 却下した理由 |
|--------|------------|
| Server push / WebSocket command | 接続中なら低遅延だが、deep sleep中は届かない。接続維持、再接続、接続数上限、認証更新の扱いがMVPには重い |
| ESP32-CAM側HTTP server | デバイスIP探索、到達性、認証、sleep復帰の扱いが複雑。サーバーからデバイスへ直接到達できる前提を置きたくない |
| MQTT broker | IoTのdesired stateには適しているが、broker運用が増え、現行Flask/SQLite構成よりMVPの運用負荷が高い |
| 任意コマンド実行API | 実装は柔軟だが、誤操作・権限昇格・秘密情報漏洩・デバイス破損のリスクが高い |
| 任意URLからの自己OTA更新 | 便利だが、署名検証、配布元認証、rollback、破損時復旧などの設計が必要でMVPには重い。初期は既存のArduinoOTA/PlatformIO OTA uploadをLAN内で実行し、非空OTAパスワードを必須にする |

## 結果・影響

- 設定変更は即時反映ではなく、次回起床または次回polling時に反映される。
- APIには、管理者向け desired config CRUD と、デバイス向け desired config 取得/ack エンドポイントが必要になる。
- metadata storeには、config version、更新時刻、最後にackされたversion、ack結果を保存する必要がある。
- ESP32-CAMファームウェアは、起床後に設定取得、範囲チェック、反映結果ackを行う処理を追加する必要がある。
- OTA更新はdeep sleep中には受けられない。更新作業時は、管理者がOTA待受要求を設定し、次回起床時の待受ウィンドウ内に同じLANから非空OTAパスワード付きでPlatformIO OTA uploadを実行する必要がある。OTAパスワード未設定・認証失敗・timeoutは失敗としてackし、待受要求を解除する。
- 将来、ライブ配信やモーター制御など低遅延制御が必要になった場合は、WebSocket command またはMQTTを別ADRで再評価する。
