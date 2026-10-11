# 開発ロードマップ

2026-10-11時点。実装の完了と実機での評価完了を分けて管理する。全体管理はオーケストレーター、領域ごとの開発と検証準備は担当PMが責任を持つ。進行管理は [#451](https://github.com/1222-takeshi/server_for_esp32cam/issues/451)。

## 現状

| 領域 | 実装・実機確認 | 残る評価・開発 |
|---|---|---|
| 撮影・保存・閲覧 | 1台のESP32-CAMからRaspberry PiへのJPEG送信、保存、ブラウザ閲覧。新着フレームだけを使うMJPEG経路 | 保存一覧の自動更新、保存時刻とライブの区別 |
| 検知・保存制御 | 人物検知、非同期推論、検知画像のみ保持。現在の実機で人物検知保存を確認 | 猫の精度評価。保存がない場合に古い画像を最新撮影と誤認させない表示 |
| 運用 | Tailscale経由SSH、Netdata、サーバー自動起動 | 7日間の実測、バックアップからの隔離復元、復旧時間の計測 |
| 遠隔設定 | desired config API、ACK、OTA。OTA書換えの実機確認あり | 現在のWebSocketファームから設定取得・反映・ACKの実機確認 |
| 拡張 | Webhook、動画upload、カメラ別管理、trackingのサーバー実装 | 通知配信の実測、ESP側の短い動画生成、複数実機、tracking実機評価 |
| 外部連携 | ローカル保存を使用 | NAS/Drive/S3、Tapo等は未実装・未評価 |

## 並列開発と受入条件

| マイルストーン / PM | 独立Issue | 完了条件 |
|---|---|---|
| [M1 閲覧](https://github.com/1222-takeshi/server_for_esp32cam/milestone/1) / 閲覧PM | [#444](https://github.com/1222-takeshi/server_for_esp32cam/issues/444) | 保存APIの結果を画面に定期反映。空・失敗状態、表示件数を明示し、ライブ経路を維持。実機画面を確認 |
| [M2 運用](https://github.com/1222-takeshi/server_for_esp32cam/milestone/2) / 運用PM | [#445](https://github.com/1222-takeshi/server_for_esp32cam/issues/445)、[#448](https://github.com/1222-takeshi/server_for_esp32cam/issues/448) | 容量・CPU・メモリ・ready・接続を低負荷で記録。隔離コピーでSQLite整合性と画像参照を検証。7日実測と復旧訓練の結果を記録 |
| [M3 遠隔設定](https://github.com/1222-takeshi/server_for_esp32cam/milestone/3) / デバイスPM | [#446](https://github.com/1222-takeshi/server_for_esp32cam/issues/446) | WebSocket運用でも設定取得・適用・ACK。現在の5秒送信を明示維持。再起動・OTA要求が一度だけ実行され、再接続を実機確認 |
| [M4 ペット評価](https://github.com/1222-takeshi/server_for_esp32cam/milestone/4) / 評価PM | [#447](https://github.com/1222-takeshi/server_for_esp32cam/issues/447) | 固定した画像・正解bbox・モデル・labelsのハッシュと設定を保存。猫あり/なしのprecision・recall、誤検知・見逃し、推論時間を実測。未提供データは未評価とする |

各PMは専用branch/worktreeを使用する。閲覧PMは画面、運用PMはscriptsと運用文書、デバイスPMはファームと遠隔設定契約、評価PMはオフライン評価ツールを担当する。境界変更はオーケストレーターが調整する。必要な専門担当だけ配置し、各PRを2名以上のread-only reviewerが確認する。BLOCKING・IMPORTANTを解消し、tests・静的解析・CIを確認した後、オーケストレーターがmergeする。

## 依存関係と次の段階

M1・M2・M3の実装は並列に進める。実機deployとOTAはオーケストレーターが順番を管理し、設定・データのバックアップを確保する。M2の観測開始日は実際のtimer稼働時に記録し、7日経過前には連続稼働を達成扱いにしない。現在の観測ツールの実装完了だけではM2は完了しない。初回の実機観測は2026-10-11 12:02:49 JST。最短の7日判定時刻は2026-10-18 12:02:49 JSTであり、その時点で欠測・ready・カメラ接続の記録を検査する。

M4の評価ツールは並列に整備できるが、現場画像と正解ラベルが揃うまでは猫検知の精度を判断できない。合格しきい値は評価前に明示し、結果を見てbaselineを変更しない。通知は設定済みでも配信成功を別途検証する。

基盤の実機評価後、検知時の短い動画、通知の有用性、複数ESP32-CAMの負荷評価を個別Issueで計画する。Tapo・外部保存先・可動部の制御は接続方式、認証、費用、必要性を調査してから着手する。サーバー側APIがあることを端末からの動画取得や外部連携の完成とは扱わない。

詳細な利用目的は [business-scenarios.md](business-scenarios.md)、遠隔設定契約は [remote-control-api.md](../design/remote-control-api.md)、運用手順は [operations-runbook.md](operations-runbook.md) を参照する。

動画APIの設計履歴は [#35](https://github.com/1222-takeshi/server_for_esp32cam/issues/35)。形式と保存契約は [docs/design/video-upload.md](../design/video-upload.md) を参照する。端末側の短い動画取得は別の実機開発・評価が必要である。
