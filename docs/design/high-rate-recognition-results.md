# Mac参考試験：高頻度撮影・猫認識の測定結果

**要件訂正（2026-10-17）:** ユーザー要件はRaspberry Pi単体での最低5Hz・希望10Hzであり、Macへの推論移動は受入対象外。本書のMac測定は参考記録として保持し、Piのマイルストーン達成扱いを撤回する。Pi単体の再検証は[#502](https://github.com/1222-takeshi/server_for_esp32cam/issues/502)、全体受入は[#486](https://github.com/1222-takeshi/server_for_esp32cam/issues/486)で継続する。

Issue [#500](https://github.com/1222-takeshi/server_for_esp32cam/issues/500)のMac参考記録。30分のMac実験では最低5Hzの速度・鮮度条件を満たした。希望10Hzは最大結果間隔条件に未達。固定条件・基準・原記録のSHA・集計値は[結果manifest](high-rate-recognition-results.json)に保存する。既存の `technical_acceptance=true` はMac実験内の判定を保持したもので、Pi要件を満たした意味ではない。本人猫の精度や追従の受入も未完了。

本番Piの現行モデルはTFLite SSD MobileNet V1系（300px uint8、class 0/person、threshold 0.5、4 thread）。Python 3.12.3、ai-edge-litert 2.1.6、NumPy 2.5.1で、モデルSHA256は `e4b118e5e4531945de2e659742c7c590f7536f8d0ed26d135abcfe83b4779d13`。約5秒周期なので新規画像は約0.2Hz。既存の137.1/137.5ms median/P95は1 threadのoffline参考値であり、現在の4 thread構成の実測値ではない。正確なexport variant・学習元は未特定。

| 試験 | 確認できたこと | 判定の限界 |
|---|---|---|
| [#489](https://github.com/1222-takeshi/server_for_esp32cam/issues/489) offline公式YOLO11n ONNX 320 | 4 threadのPi 4 median/P95 136.46/198.29ms、Mac 6.79/7.12ms。公開training 20枚の参照比較はthreshold 0.25 | 撮影・転送・時計・queueを含まない。本人猫・夜間精度や実運用Hzは未判定 |
| [#494](https://github.com/1222-takeshi/server_for_esp32cam/pull/494) adapter互換性 | adapterと参照をthreshold 0.5に揃えると、両者9件の猫検出 | 同一公開画像での互換性であり、現場GTによる精度ではない |
| 実カメラ100ms・120秒の撮影単体（`20261016T232333Z-e11f32a1-capture`） | 9.708Hz、stale 4件 | 10Hz未達。推論完了数ではない |
| 初回ONNX結合試験（`20261017T062014Z-59e4031e-onnx`） | clock timeoutによる再接続で結果間隔2,555.936msとなり中断 | 失敗runとして保存。30分受入は未達で、後続runと合算しない |
| 95ms・Wi-Fi省電力なし・120秒の撮影/受信（`20261017T062853Z-c2627e26-capture`） | 10.508Hz、撮影・受信・accepted一致、drop 0、wire gap 0 | この条件の短時間の転送確認。30分の推論・鮮度・負荷・精度の合格ではない |

## Macでの30分ONNX参考試験

固定runは `20261017T063151Z-9e15f03a-onnx`。Git外のprivate manifest/criteriaと開始時source archive、閉じたJSONL/footerと派生集計を保存し、原記録を変更せず照合した。ログ・画像・認証値・モデル本体はGitへ保存しない。
途中の `20261017T062801Z-12be8fbd-capture` は構成取り違えのため **INVALID-SETUP** として原記録を保持し、95ms候補の採用値には使わない。

| 固定条件 | 値 |
|---|---|
| 対象 | 1台 `esp32cam-01`、隔離WebSocket受信器、初回acceptedから30秒warmup後に1,800秒計測 |
| 計測窓 | 2026-10-17 06:32:21.452727〜07:02:21.452727 UTC（15:32〜16:02 JST）、左閉右開 |
| ファーム候補 | 95ms、`WIFI_PS_NONE`、binary SHA256 `5729768036dcfce2c7ef0e15124648883896dc0ea56e17a64658037e1e936142` |
| 推論 | 公式YOLO11n ONNX 320、cat class 15、threshold 0.5、4 thread、model SHA256 `ff1013da8734fe2b8ede5f34a2124855d447fbefd248d148e9f29e7f8e28c9c4` |
| runtime | Python 3.12.15、ONNX Runtime 1.23.2、numpy 2.2.6、websockets 17.2、Pillow 12.3.0 |
| 時計・鮮度 | 同一bootのclock sampleとoffset区間を照合。200ppmは検証対象の仮定。capture→resultの保守的な上限を500ms以内とする |
| 資源 | 5秒間隔の資源記録、30秒間隔の`pmset`記録。Mac実温度は独立sensorがなく不明 |
| 固定基準 | criteria SHA256 `7c3691061dcd697057637efc0b94574a2223ede5aa08e08d85f52d2edd4b9118`。詳細はprivate `criteria.json`に固定 |

受入基準は**新規で鮮度内に正常完了した別フレーム**の全1,800秒平均と各10秒窓の両方で最低5Hz、希望10Hzを別判定する。希望10Hzは全窓も10Hz以上であり、平均10Hzだけへ読み替えない。最低基準には最大成功間隔500ms、capture→result上限P95 250ms・最大500ms、drop率とwire欠落率各1%以下、推論失敗0、時計整合、warmup後RSS増加32MiB以下、平均process CPU 300%以下を含む。希望基準の最大成功間隔は250ms。分母・窓端の扱いは固定criteriaを使い、結果を見て変更していない。

| 指標 | 確定結果 |
|---|---|
| 新規撮影観測・受信・accepted・推論開始・鮮度内正常完了 | 各18,935件、各10.519444Hz。ID・撮影時刻の重複0 |
| 各10秒窓 | 180窓、103〜106件、最低10.3Hz・最高10.6Hz。合計18,935件 |
| 最大正常完了間隔（窓端を含む） | 409.891ms。最低の500ms以内、希望の250ms超過 |
| 撮影→結果の保守的な遅延上限 | P50 78.617ms、P95 95.491ms、最大425.034ms。TTL超過成功0 |
| drop / wire欠落 / 推論失敗 | 計測窓では各0。warmup前のclock未確定フレームは成功数へ含めない |
| 再接続 | 再接続を示す記録なし。接続開始/終了を直接記録していないため回数は未計測 |
| 時計 | 同一bootで874 sample、200ppm仮定とoffset区間は整合。midpoint回帰の差は16.334ppmで、独立した物理校正の代わりにはしない |
| Mac資源 | warmup後RSS増加0.5MiB、最大106.45MiB、平均process CPU 98.445%（1 core=100%、時間coverage 99.946%）。`pmset`にthermal/performance warning記録なし。実温度は不明 |
| 同時Pi health観測 | 359 sample、health/ready計718回すべてHTTP 200・ready。遅延P95 10.907ms・最大44.063ms、温度最大49.66℃、throttled=0x0 |
| Mac参考試験の最低5Hz / 希望10Hz | Mac実験内の固定速度・鮮度基準はpass。Pi要件は未受入。希望10Hzは最大間隔だけfail |
| 原記録整合性 | 41,575 events、25,341,710 bytes。閉じたJSONLのSHA、run/criteria SHA、最終窓/footer hash等の全guardが一致 |

正常完了は推論終了時刻、受信/dropの分母は受信時刻で `[start,end)` に分類する。窓開始前に受信して窓内に完了した1件を含み、窓内に受信して終了後に完了した1件は成功数に含めない。終了時の実行中は1件。wire gapは同一boot/sequenceから一度だけ数え、boot/接続境界の不明な欠落を0と推定しない。このrunは同一bootでsequenceが連続し、clock timeoutを示す記録もないが、短時間の同一boot再接続回数は直接測定していない。

時計誤差を含む遅延は200ppmという事前仮定付きの上限で、時刻校正そのものの保証ではない。推論成功は猫0件の正常結果も含み、18,935件の猫検出を意味しない。現場GTがないので猫のprecision/recallは評価していない。Piのhealthは定期sampleであり、連続uptimeや通常画像uploadとの同時処理を証明しない。Pi観測との窓合わせはUTCによるもので、ホスト間UTC誤差は独立測定していない。Macでは同じ試験中にローカルCIも動作したが、制御した負荷試験ではなく、最大間隔の原因は特定していない。

## 通常運用への復帰と次段階

通常camera→Piの最後の確認済みフレームは2026-10-16 23:24:55 UTC、実験ファームへの切替は23:25:22 UTC（10月17日08:25 JST）。作業中断後、2026-10-17 06:19 UTCに再開した。試験終了後のwatchdogは復旧OTAの1回目で転送エラーとなり、2回目で通常ファーム（SHA256 `512feb5bc146bd33090664e7786a1eaf98411a1d6dd809562f52a1d99c0d596b`）への復帰に成功。元の5秒/QVGA/jpeg12/OTA要求なし設定v9は07:19:37 UTCにapplied ACKを返した。停止時間や復旧時間をhealth正常で代替せず、最初の画像再開時刻は未確定として残す。

11:38:29〜11:38:46 UTCの独立した3回の確認で、受信フレーム数が99,641→99,642→99,644、最終フレーム時刻が11:38:29→11:38:34→11:38:44 UTCへ進み、通常画像更新とhealth/readyを確認した。設定ACKと画像更新は別証拠で記録している。M2/7日observerの元データは変更・除外しない。実験中の通常画像停止を運用成功に読み替えない。

本人猫・猫なし・昼・消灯の独立GT、Mac実温度、physical calibration、常用deployment、モータなし角度simulation、駆動のいずれも未受入。[#486](https://github.com/1222-takeshi/server_for_esp32cam/issues/486)はこれらの残件を含むためopenを維持し、[#451](https://github.com/1222-takeshi/server_for_esp32cam/issues/451)のM2/M4全体完了とは区別する。次段階は希望10Hzの間隔改善、GT/夜間撮像方式、校正と角度simulation、既存[猫追従契約](cat-following-capability.md)の鮮度/停止条件、通常運用との同時負荷・常用配置を個別に検証する。
