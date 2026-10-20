# Pi Wi-Fi省電力OFF後の再測定

関連: [#538](https://github.com/1222-takeshi/server_for_esp32cam/issues/538)、
[#506](https://github.com/1222-takeshi/server_for_esp32cam/issues/506)、
[前回の反映・不合格記録](operating-deployment-results.md)。

## 比較前提

利用者がPiで `iw` をinstallして `power_save off` を実行した後、2026-10-20 14:54:48 UTCに
`Power save: off` を確認した。サービスやfirmwareを更新・再起動せず、同じsource
`badfe24beba46086f6fca20742e4fdb45f7f47d7`、camera boot、native PID/start_ticksを確認した。
モデルはYOLO11n FP32 ONNX320、threshold0.5、ORT3threads、QVGA/JPEG12のまま。
コード206ファイルとモデル全体をSHA照合し、前回と同じ不変 `acceptance-v6.py` を使用する。
Macは操作端末のみ。motorはhold/observe_onlyに留める。

SSH/PAMやpackage操作後に60秒以上settleし、同じ30秒warmup後1800秒の閲覧・隔離した模擬保存/通知を実行する。
前回と同じ5Hz・P95年齢上限250ms・最大gap500ms・drop1%等の基準を維持する。
raw/helper/失敗run/基準値を変更せず、各runの完了後に結果を記載する。

Wi-Fi設定変更の正確な時刻は未記録。aptによるpackage導入と測定日・環境の差があり、
前回動作していたread-only audit2本も今回は既に終了している。設定OFF後の運用測定として扱い、
省電力だけが原因・対策だったとの厳密な因果関係を主張しない。
`iw` のOFFは実行時の観測であり、再起動後も維持されるOS設定の受入は未実施。

## 再測定結果

最初のOFF後run `acceptance-20261020T145842Z` は414.230秒で `resource_thermal_fault` により不合格。
5.2–5.3Hz・結果gap最大329.703ms、preview欠落0、模擬保存/通知45件・監視異常0だったが、
80.341℃に達した。throttleは0、同じbootのeco active 0.199989Hz/ACK/holdまで復帰確認した。
この時間内で前回の長い通信途切れは再発しなかったが、30分条件は未達のままである。
待機threadのspin抑制は[#539](https://github.com/1222-takeshi/server_for_esp32cam/issues/539) /
[PR #540](https://github.com/1222-takeshi/server_for_esp32cam/pull/540)でnative workerだけに実装した。
モデル、前後処理、3threads、撮影設定、判定基準は維持。汎用detectorの既定設定も維持する。
Piの固定20画像・各3回はaccepted baseline・両設定間で検出数/クラス/座標/スコアの最大差0。
短時間のhelper集計CPU249.088→192.257%、推論中央値150.121→153.209msだが、
個別60回の時間/rusageは未保存のため時間集計は独立再計算できず、長時間・実W数へ外挿しない。
保存された20出力とbaselineは独立照合済み。現場の猫/昼/消灯GTによるprecision/recallは別途必要。

## スピン抑制後の30分同時負荷（合格）

merge `31a96a5dae95d1f92d6c3de813fc62168e7c8ee0` を208ファイル照合してPiに反映し、
`update-20261020T152212Z` の同一boot・新native PID675239でeco→tracking→ecoを確認した。
同じ不変v6を再実行した `acceptance-20261020T152531Z` は、30秒warmup後の
**1,800.080777秒・9,474 fresh results・平均5.263097Hz**で合格した（JST 10/21）。

| 確認項目 | 測定値 |
|---|---:|
| 観測したnative完了10秒bucket | 5.2–5.3Hz |
| 計測区間の年齢histogram差分P95上限 / 世代内最大結果gap | 225ms / 359.564ms |
| 区間drop / 推論失敗 | 0 / 0 |
| native CPU平均（1Hz sample）/ RSS観測増加 | 209.244561% / 618,496 bytes |
| 最高温度 / throttle | 76.445℃ / 全sample 0 |
| health成功 / preview成功・欠落 / 画面閲覧 | 3,600 / 3,597・0 / 180 |
| health P95 / 最大 | 14.796881ms / 107.536736ms |
| 区間内の模擬代表保存 / localhost通知成功 | 178 / 178 |

保存・通知は固定RAM JPEGと明示した模擬cat detectionによる**隔離SQLite/画像/Webhook**への負荷。
実カメラの推論・RAM preview・常用側の検出時writerは同時に動くが、178件は実猫検出数ではない。
全1,800 sampleはactive、writer/monitor異常0。最後に同じboot/PIDで
**eco active 0.199901Hz、ACK、hold**へ復帰し、cleanup異常もない。
postflightでもWi-Fi OFFを実測した。今回の条件で同時負荷30分を通過した結果であり、
周囲温度を変えた試験や長時間連続trackingの保証にはしない。

再計算はfresh counter差分・年齢histogram9,474件・1,800行のCPU/温度/RSSから行い、
raw・失敗run・旧baselineを保持する。health個別latencyはhelper集計のみで、個別配列は保存していない。
postflightの常用ログ合計4,471,701 bytes。privateのrelease/backup/試験領域372,305,920 bytesは
常用ログ上限の対象外で、自動削除やDB VACUUMは実施していない。

#503全体の再受入、現場precision/recall、10Hz認識、W数、motor駆動とは区別する。

## ecoの短時間CPU観測

最後のSSH/SCPから60秒以上待ち、同じnative PID/start_ticks/世代・eco activeの
開始/終了を照合した101.078487秒で20 frames / 20 fresh results、**0.197866Hz**。
Linux `/proc/PID/stat` のCPU tick差875（100 ticks/s）よりnative CPU平均は**8.656639%**。
開始/終了値と計算式をprivate原本に保持する。以前のsource badfe24の91.788秒・11.134297%とは
時間・source・温度が異なる短時間観測であり、実W数や厳密な省電力量・24h平均は未測定。

## 完了した24時間観測（設定変更前）

前回のread-only audit2本は実時間24時間に到達し、resultファイルを確認した。
いずれもWi-Fi設定変更前の複数release・実際の手動モード・保護状態を含む観測で、
24時間連続trackingや全画像猫ありの負荷試験ではない。

| audit | 経過秒 | 取得成功/エラー | active / degraded / pending の観測行 |
|---|---:|---:|---:|
| v1 10/19 00:04:49 UTC開始 | 86,400.057 | 1,440 / 1 | 907 / 530 / 3 |
| v2 10/19 05:25:50 UTC開始 | 86,400.107 | 1,441 / 0 | 897 / 542 / 2 |

行数の比は実時間uptimeではなく、HTTP readyと認識状態の正常も同義ではない。
v2は31dca86/c1bda0b/badfe24の418/20/1,003行。serviceの実write_bytes差分は
18,378,752 bytesで、退出17プロセス間の書込みを取りこぼし得る下限。全hostの
1,857,511,424 bytesとは区別する。v1も同様に20,221,952 bytes（退出25、下限）と全host2,065,559,552 bytes。
保護中の任意書込み停止を含むため、通常負荷の24時間書込み合格へ推計しない。

v2の観測最大DB32,104,448/WAL663,352/常用ログ3,176,987 bytes、最小空き23,526,629,376 bytes。
journal観測はv1 2,805/v2 1,126 bytes、いずれも8MiBで切り詰めなし。
物理hard quota・SD寿命や省電力OFF後の24h合格を保証する数値ではない。

## 新しい24時間観測

`audit24h-20261020T155722Z`（v3）をread-onlyで開始し、最初のsampleは同じsource/boot・
eco active・Wi-Fi OFFを確認した。helperはMac/Piで9テスト成功、2名reviewのB/I/M各0。
60秒ごとにsource/PIDの前後照合、camera boot、実測したWLAN power-save状態、DB/WAL/log/書込みを
記録する。証拠上限8MiB、24hで停止し、mode・OS設定・rawを変更しない。
この開始記録は完了・通常負荷24h・OFF後24hの合格ではない。実W数、現場precision/recall、
低頻度での見逃し、10Hz認識、実motorは引き続き[#505](https://github.com/1222-takeshi/server_for_esp32cam/issues/505) /
[#506](https://github.com/1222-takeshi/server_for_esp32cam/issues/506) /
[#486](https://github.com/1222-takeshi/server_for_esp32cam/issues/486)の残項目として扱う。
