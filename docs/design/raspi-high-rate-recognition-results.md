# Raspberry Pi単体の高頻度猫認識

ユーザーの必須条件はRaspberry Pi単体。実装・検証は[#502](https://github.com/1222-takeshi/server_for_esp32cam/issues/502)、PRは[#503](https://github.com/1222-takeshi/server_for_esp32cam/pull/503)。[Mac参考試験](high-rate-recognition-results.md)はPiの達成対象外とし、原記録を保持している。

## 高速化した箇所

通常運用の整数秒設定は5秒が下限なので、専用targetで190ms撮影・2フレームバッファ・最新画像取得・Wi-Fi省電力解除を使う。Pi側は全画像のファイル/SQLite保存を待たず、メモリ上で前処理・推論を進める。実行中1枚と最新待機1枚に制限し、上書きや失敗は成功数へ加算しない。撮影/受信とnative推論を別threadにして、既存geventのrequest処理を塞がない。

同じYOLO11n FP32モデルのPi上のthread数は2から3へ変更した。固定公開画像40回のmedian/P95は169.932/174.874msから145.342/167.059msになった。これはoffline比較であり、実運用Hzは以下の結合試験で判断する。RAW時計への変更はNTP補正による計測の歪みへの対処であり、推論速度を上げる変更ではない。

通常本番のSSD/personから実験のYOLO/catへは検出対象もモデルも変わるため、「従来の人物モデルと同等の猫精度」とは主張しない。Mac参考試験からのモデル軽量化・量子化・入力縮小・しきい値緩和は行っていない。

## 固定条件

| 項目 | 値 |
|---|---|
| 機材・処理場所 | 既存Raspberry Pi 4 CPUで受信・JPEG前処理・猫推論・結果処理。MacはSSH操作と結果確認のみ |
| 撮影 | ESP32-CAM 1台、QVGA 320×240、JPEG品質12、190ms候補、2バッファ、Wi-Fi省電力なし |
| モデル | 同じ公式YOLO11n FP32 ONNX、320入力、cat class 15、threshold 0.5、3 thread。量子化・モデル小型化なし |
| モデルSHA256 | `ff1013da8734fe2b8ede5f34a2124855d447fbefd248d148e9f29e7f8e28c9c4` |
| runtime | Python 3.12.3、ONNX Runtime 1.23.2 CPUExecutionProvider、NumPy 2.5.1、Pillow 12.2.0、websockets 17.2 |
| 計測 | 初回acceptedからwarmup30秒、その後1,800秒。各10秒窓・窓端込みの最大結果間隔も判定 |
| 時計 | 受信・推論・event・窓・温度/health計測をCLOCK_MONOTONIC_RAWで統一。200ppm仮定とoffset区間を検証 |
| 固定criteria SHA256 | `421236d1f80991b41adcaa0824fa400ce460bdbbafd2441ff764be90ec0964a1` |
| runtime commit | `01111551768f327a9561504166a6a770d8c57f28`。試験前のsource archiveもGit外へ保存 |

最低基準は全体と各10秒窓で5Hz以上、最大結果間隔500ms、撮影→結果の保守的上限P95 250ms・最大500ms、drop/wire欠落各1%以下、推論失敗0、時計整合、RSS増加32MiB以下、平均process CPU300%以下。Pi温度80℃未満・throttle0、5秒ごとの新規health試料357件以上・最大間隔6秒、health/ready全200/ready・遅延P95 250ms以下/最大2秒以下も固定した。希望10Hzは全体・各10秒窓10Hz以上と最大結果間隔250msを別判定する。

## 先行試験

| run | 結果 | 扱い |
|---|---|---|
| `20261017T124248Z-6d90c9d9-onnx` / 2 thread・通常MONOTONIC・120秒 | 630件、5.25Hz、遅延上限P95 259.163ms。clock200ppm不整合 | 不合格で保存。後続runと合算しない |
| `20261017T125300Z-b290131b-onnx` / 3 thread・RAW・120秒 | 631件、5.258333Hz、各10秒窓5.2Hz以上。遅延上限P95/max 213.418/246.645ms、最大結果間隔226.341ms、wire欠落0、終了時cancelled 1、時計200ppm整合 | 短時間の接続確認。30分受入には含めない。元設定v13 ACK後の新フレーム更新まで自動復帰を確認 |

通常MONOTONICとRAWの独立比較で、Piの時計補正による約330ppmの差を観測した。RAWはNTP補正の対象外であり、200ppm基準を緩めず測定軸を揃えた（[Pythonの仕様](https://docs.python.org/3/library/time.html#time.CLOCK_MONOTONIC_RAW)）。時計変更はモデルの高速化とは別の測定修正である。

## 30分試験

固定runは `20261017T125927Z-f8512e62-onnx`。**Pi単体の最低5Hz技術基準を通過**。原記録と結果のSHAは[結果manifest](raspi-high-rate-recognition-results.json)へ記録する。

| 指標 | 30分の結果 |
|---|---|
| 新規認識正常完了 | 9,472件、平均5.262222Hz。猫0件の正常結果も含む |
| 各10秒窓 / 最大結果間隔 | 180窓で5.1〜5.3Hz、窓端込み368.530ms |
| 撮影→結果の保守的遅延上限 | P50 207.258ms、P95 231.765ms、最大471.638ms |
| 撮影/受信/accepted | 各9,473件、5.262778Hz。成功数と区間端が異なる |
| drop / wire欠落 / 推論失敗 | drop2件（上書き1・終了時cancelled1）、約0.0211%。wire欠落0、推論失敗0 |
| 時計 | 871試料、200ppm仮定とoffset区間が整合。midpoint fit 37.742ppm。物理校正の代替ではない |
| Pi資源 | 平均process CPU250.059%（1 core=100%、時間coverage99.837%）、warmup後RSS増加2.1875MiB |
| Pi温度 / API | 最大75.958℃、throttled=0x0。360試料、health/ready計720回すべて200/ready、遅延P95 19.898ms・最大90.273ms |
| 最低5Hz / 希望10Hz | 最低5Hz技術条件pass。190ms撮影は10Hz入力ではないため、次節で別診断する |

Pi-local systemd supervisorがlistener確認、復旧arm永続化、OTA設定/ACK、候補flash、時間制限付き試験、元設定・通常FWへの復旧を管理した。SSH切断から独立し、終了後はrollback初回成功、元設定一致を確認。元5秒/QVGA/jpeg12設定v15のapplied ACKは13:31:08.999 UTC、その後baseline100540から100541へ増加し、新フレーム13:31:16.050 UTCまで確認した。別のPi-local auditorが終了後の集計とSHA記録を行った。通常画像の停止をM2元観測から除外しない。

## 希望10Hzの短時間診断

最低5Hz試験の復旧確認後、同じPi・モデル・画像条件で95ms撮影、warmup30秒＋120秒を別run `20261017T133240Z-90a715f3-onnx` として測定した。**10Hz認識は未達**。30分受入と合算せず、短時間のdesired条件と30分acceptanceを分ける。

| 指標 | 120秒の診断結果 |
|---|---|
| 撮影/受信/accepted | 各1,263件、10.525Hz |
| 認識正常完了 | 829件、6.908333Hz、各10秒窓6.9〜7.0Hz |
| 最大結果間隔 / 遅延上限 | 195.188ms / P95 298.448ms・最大323.131ms |
| drop / wire欠落 / 推論失敗 | 435件（上書き433・終了時cancelled2）、34.4418%。wire欠落0、推論失敗0 |
| 資源 / 時計 | 平均process CPU299.707%、RSS増加0.4375MiB、最大74.984℃、throttle0、48回health/ready全200。200ppm仮定と整合 |

撮影側は10Hzを超えたが、認識側は約6.9Hzで頭打ちになった。最大間隔250msだけは満たしても、全体/各窓10Hzを満たさない。5Hz基準の遅延P95 250ms・drop1%も超えた。短時間診断のため30分guardも不成立であり、基準を緩めて合格させていない。どちらのrunも、受信が窓前で推論完了が窓内の1枚を含み、窓末尾の処理中画像は後のcancelledをdropへ含めるため、受信数・成功数・drop数の単純な差引きにはならない。

このrunも通常FWへの復帰は初回成功。元設定v17のACKは13:36:21.945 UTC、ACK後baseline100563から100564・新フレーム13:36:28.822 UTCを確認し、13:36:33.292 UTCに復旧完了した。その後作業は利用上限で中断したが、再開時2026-10-17 23:33:33〜43 UTCの3回の独立probeでもframe107719→107721と時刻の更新、health/ready 200を確認した。これは全中断時間の連続稼働証明ではない。

## 認識精度と残件

公開training20枚をPiの2/3 threadで比較し、猫検出9件のclass/countが一致、score/正規化座標の最大差は6.56e-7。これは同じFP32モデルのthread間互換性確認であり、現場のprecision/recallではない。認識完了数は猫0件の正常結果も含む。モデル・画質設定を維持しても、高頻度撮影時の露光・ブレを含む本人猫/暗所の精度は未評価である。

照明状態は未確認・操作なし、既存の自動露光設定を使用。全画像の永続保存は行わず、通常upload/保存/通知との同時負荷・常用統合は未受入。物理時計校正、本人猫/猫なし・昼/消灯GT、モータなし角度simulationも[#486](https://github.com/1222-takeshi/server_for_esp32cam/issues/486)で継続管理する。通常画像の停止と復帰はM2の元観測から除外しない。
