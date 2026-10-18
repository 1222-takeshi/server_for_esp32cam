# 開発ロードマップ

2026-10-17時点。実装の完了と実機での評価完了を分けて管理する。全体管理はオーケストレーター、領域ごとの開発と検証準備は担当PMが責任を持つ。進行管理は [#451](https://github.com/1222-takeshi/server_for_esp32cam/issues/451)。

## 現状

| 領域 | 実装・実機確認 | 残る評価・開発 |
|---|---|---|
| 撮影・保存・閲覧 | 1台のESP32-CAMからRaspberry PiへのJPEG送信、保存、ブラウザ閲覧。新着フレームだけを使うMJPEG、保存画像のpolling、ライブと保存の区別、非表示タブの初回取得、不変JavaScript cache対策 [#462](https://github.com/1222-takeshi/server_for_esp32cam/issues/462) を実装。SafariでのPi画面の最終確認は18:14 JSTに完了 | 更新後の回帰監視を継続 |
| 検知・保存制御 | 人物検知、非同期推論、検知画像のみ保持を実機確認。公開猫画像でYOLO11nと追加学習候補を比較、猫なし室内34枚で5モデルの誤検出0件を確認 | 本人猫・消灯環境のGT評価、本番runtime互換性・同時負荷は未達。公式モデルを基準、007を比較候補として保持し、本番採用は未決定 |
| 撮影・認識速度 | 通常経路は人物SSD・約5秒周期。Pi 4単体の隔離経路で30分9,472件・平均5.262222Hz、各10秒窓5.1〜5.3Hz、遅延上限P95 231.765msとなり最低5Hz技術条件を通過。[Pi結果](../design/raspi-high-rate-recognition-results.md)・[PR #503](https://github.com/1222-takeshi/server_for_esp32cam/pull/503)を参照 | 希望10Hzは未達。別120秒診断は撮影/受信10.525Hz・認識6.908333Hz、drop34.44%。本人猫/猫なし・昼/消灯GT、校正、通常保存/通知との同時負荷・常用統合を[#486](https://github.com/1222-takeshi/server_for_esp32cam/issues/486)で継続。Mac試験は参考のみ |
| 容量・運転モード | [設計契約](../design/operating-modes.md)を追加。手動eco（候補5秒）/追跡準備（5Hz）を同じモデル・画質で分け、認識・画像保存・ログ頻度を独立させる。まだ未実装 | [#505](https://github.com/1222-takeshi/server_for_esp32cam/issues/505)で容量/書込み/待ち行列制限を先行し、[#506](https://github.com/1222-takeshi/server_for_esp32cam/issues/506)で共通firmware・ACK/新frame確認付き切替。実W数・低頻度の見逃しと保存/通知同時負荷を評価。motorは別受入まで停止 |
| 運用 | Tailscale経由SSH、Netdata、サーバー自動起動、5分ごとの観測timer。隔離コピーのSQLite整合性と画像参照1,002件、隔離API・画像のHTTP 200を確認 | 完全復旧のRTO未測定。7日集計は2026-10-18 12:03 JSTに予約済みで結果待ち |
| 遠隔設定 | WebSocketファームでdesired config取得・適用・ACK。実機で5秒QVGA送信を維持してOTA更新、ACK、再起動、OTA timeout、6秒VGAの実寸、通常QVGAへの復帰ACKを確認 | 電断などを跨ぐone-shot処理のexactly-onceは保証しない。不確実な場合は状態確認後に新versionで手動再要求 |
| 拡張 | Webhook、動画upload、カメラ別管理、trackingのサーバー実装 | 通知配信の実測、ESP側の短い動画生成、複数実機、tracking実機評価 |
| 外部連携 | ローカル保存を使用 | NAS/Drive/S3、Tapo等は未実装・未評価 |

## 並列開発と受入条件

| マイルストーン / PM | 独立Issue | 完了条件 |
|---|---|---|
| [M1 閲覧](https://github.com/1222-takeshi/server_for_esp32cam/milestone/1) / 閲覧PM | [#444](https://github.com/1222-takeshi/server_for_esp32cam/issues/444)、[#460](https://github.com/1222-takeshi/server_for_esp32cam/issues/460)、[#462](https://github.com/1222-takeshi/server_for_esp32cam/issues/462) | 保存polling・ライブ区別・非表示タブ初回取得・不変cache対策とPi画面の最終確認が完了。更新後の回帰を監視する |
| [M2 運用](https://github.com/1222-takeshi/server_for_esp32cam/milestone/2) / 運用PM | [#445](https://github.com/1222-takeshi/server_for_esp32cam/issues/445)、[#448](https://github.com/1222-takeshi/server_for_esp32cam/issues/448)、[#456](https://github.com/1222-takeshi/server_for_esp32cam/issues/456)、[#458](https://github.com/1222-takeshi/server_for_esp32cam/issues/458) | 観測・隔離検査は実装・実機確認済み。7日間の欠測・ready・接続の集計結果と完全復旧RTOを記録する |
| [M3 遠隔設定](https://github.com/1222-takeshi/server_for_esp32cam/milestone/3) / デバイスPM | [#446](https://github.com/1222-takeshi/server_for_esp32cam/issues/446) | WebSocket設定と主要な実機経路を確認済み。one-shotは電断時のexactly-onceを保証せず、状態確認・新version要求の運用を維持する |
| [M4 ペット評価](https://github.com/1222-takeshi/server_for_esp32cam/milestone/4) / 評価PM | [#447](https://github.com/1222-takeshi/server_for_esp32cam/issues/447)、[#451](https://github.com/1222-takeshi/server_for_esp32cam/issues/451) | 公開画像での比較は記録済み。本人猫・猫なし・昼/消灯環境の別セッションGTと本番runtimeでprecision・recall、誤検知・見逃し、処理時間を評価する |
| 高頻度撮影・認識 / デバイス・推論PM | [#484](https://github.com/1222-takeshi/server_for_esp32cam/issues/484)の計画・[#502](https://github.com/1222-takeshi/server_for_esp32cam/issues/502)のPi検証・[#486](https://github.com/1222-takeshi/server_for_esp32cam/issues/486)の全体受入 | Pi単体の最低5Hz技術条件pass、希望10Hz未達。精度、常用同時負荷、校正と角度シミュレーションは残件として#486をopenのまま管理する |

各PMは専用branch/worktreeを使用する。閲覧PMは画面、運用PMはscriptsと運用文書、デバイスPMはファームと遠隔設定契約、評価PMはオフライン評価ツールを担当する。境界変更はオーケストレーターが調整する。必要な専門担当だけ配置し、各PRを2名以上のread-only reviewerが確認する。BLOCKING・IMPORTANTを解消し、tests・静的解析・CIを確認した後、オーケストレーターがmergeする。

## 依存関係と次の段階

M1～M3の主要な実装とM4の評価ツールは反映済み。実機deployとOTAはオーケストレーターが順番を管理する。M2初回観測は2026-10-11 12:02:49 JST、凍結ログの集計timerは2026-10-18 12:03 JSTに設定済みで、7日経過前は達成扱いにしない。集計のサンプル比率は実時間uptimeを証明しない。バックアップのSQLite整合性・画像参照と隔離HTTP応答は確認したが、完全復旧のRTOは未測定である。

M1の最終画面確認では2026-10-11 18:14 JSTにSafariでPiの画面を再読込し、保存画像989件、最新保存16:48:45 JST・検知1件、対象なし画像を保存しない説明、5秒の想定間隔と約5秒のライブ表示、healthy/readyを確認した。保存件数と最新時刻はAPIと一致し、ライブ画像は保存画像と別に表示された。

M4の評価ツールは実装済みだが、現場画像と正解ラベルが揃うまでは猫検知の精度を判断できない。合格しきい値は評価前に明示し、結果を見てbaselineを変更しない。通知は設定済みでも配信成功を別途検証する。

高頻度の隔離撮影・転送・鮮度計測・メモリ上推論はPi単体の最低5Hz技術条件を通過した。Macへのオフロードによる達成扱いは撤回したままとする。同じYOLO11n FP32 ONNX 320・threshold0.5、QVGA/JPEG12を維持したが、公開画像のthread互換性を現場精度へ読み替えない。次は通常運用との統合・同時負荷、本人猫/猫なし・昼/消灯GTと夜間撮像、Pi推論の追加最適化による10Hz評価、physical calibration、モータなし角度シミュレーションを個別に進める。毎秒の認識回数へ追跡の補間を加算しない。機材や追加購入は未決定で、駆動前には高頻度認識と既存の安全条件の両方を満たす必要がある。

実験ファームへの切替後、2026-10-16 23:25:22 UTCから通常camera→Piの画像更新は停止し、作業は中断後2026-10-17 06:19 UTCに再開した。watchdogの復旧OTAは1回目転送エラー、2回目で成功。元の5秒/QVGA設定v9は07:19:37 UTCにACKし、11:38 UTCの3回の独立確認で通常受信フレーム数と時刻の更新を確認した。最初の画像再開時刻は未確定。M2/7日observerの元データは変更・除外せず、health正常を通常画像の連続取得の証拠とは扱わない。

基盤の実機評価後、検知時の短い動画、通知の有用性、複数ESP32-CAMの負荷評価を個別Issueで計画する。Tapo・外部保存先・可動部の制御は接続方式、認証、費用、必要性を調査してから着手する。猫を画面中心へ向ける将来の可動部制御は[猫追従の能力契約](../design/cat-following-capability.md)に安全条件と未決事項を記録する。サーバー側APIがあることを端末からの動画取得や外部連携の完成とは扱わない。

詳細な利用目的は [business-scenarios.md](business-scenarios.md)、遠隔設定契約は [remote-control-api.md](../design/remote-control-api.md)、運用手順は [operations-runbook.md](operations-runbook.md) を参照する。

動画APIの設計履歴は [#35](https://github.com/1222-takeshi/server_for_esp32cam/issues/35)。形式と保存契約は [docs/design/video-upload.md](../design/video-upload.md) を参照する。端末側の短い動画取得は別の実機開発・評価が必要である。
