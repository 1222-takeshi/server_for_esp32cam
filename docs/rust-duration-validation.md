# Piでの有限長時間検証（2026-10-26 UTC）

Issue #573の追試として、マスク修正FWと受信除外の数値診断を使い、
Raspberry Pi 4で10分のeco→tracking→eco試験を完了しました。続く1時間指定試験は約19分で保護停止しました。
[機械可読の条件・結果・raw SHA](rust-duration-validation.json)を併記しています。
初回の中断記録は[rust-long-trial-validation.md](rust-long-trial-validation.md)に保持しています。

## 10分の結果

600秒3158件、5.2633Hz、全60区間の10秒窓は52〜54件でした。
最大公開間隔327.615ms、capture age上限はP95 217.503ms・最大341.867ms、
compute P95は165.366msでした。全sampleのboot/世代/順序/ageと全窓・端のgapも別のPython処理で再計算しました。

受信3218件を全件採用し、受信除外・overwrite/busy・ACK拒否・推論失敗・証拠欠落は0件でした。
省エネへ切り替えた後に完了した旧tracking世代1件は`mode_after`として破棄し、
評価区間外に記録しています。最大温度74.010℃、throttleは全635記録0、native peak RSS48.918MiBでした。

元FWへの2段階復旧を含むOTA計3回は、port58119/34113/34397で各初回成功しました。
元Python `613d293`・元unit・元FW `feed55ed...`、eco active/ACK/hold/readyへ復旧し、guard cleanupまで確認しました。
選択した証拠16ファイルの合計は552,820bytesで、そのうちnative reportは461,410bytesです。
JPEG・設定・鍵・大きなバイナリはGitへ含めず、rawをprivate領域に0400で保全しました。

## 固定165ms予約での1時間指定試験

約1135回の資源監視記録後に`mode_degraded / frames_expired`で中断しました。
5960件を全件採用し、受信事前除外は0、待ち画像のoverwriteは3件でした。
workerは5956件を処理・5954件を公開し、推論失敗・証拠欠落は0件です。
計測区間が完了していないため、1時間の5Hz合格や平均Hzは算出していません。

sequence5960は到着age上限52.001ms、待ち163.149ms、compute299.172msで、
公開前のage上限514.322msとなり破棄されました。直前のcomputeも274.715msに増加していました。
次の結果が完成した時点では前回公開から556.934ms経過し、保護後のcancelとして記録されています。
ここでいうcomputeはJPEG decode・前処理・推論・後処理を含みます。
この停止では到着後の待ちと処理時間が直接の制約ですが、処理時間増加のOS/CPU要因は未確定です。

最高温度76.445℃、throttleは全記録0、peak RSS49.418MiB。
元FWへの2段階復旧・元Python/unit・eco ACK/hold/ready・guard cleanupを確認しました。
選択した証拠16ファイルの合計は994,449bytesで、そのうちnative reportは847,101bytesです。失敗回も保全しています。
## 適応予約での10分指定試験

[PR #585](https://github.com/1222-takeshi/server_for_esp32cam/pull/585)のopt-in予約は、直近2秒・最大32件の
compute最大値へ25%を足し、165〜400msへ制限します。同じstrict受入条件で試しましたが、
49回の資源監視記録後に`mode_degraded / frames_expired`で中断しました。
170件を受信・採用・推論・公開し、受信除外・worker破棄・推論失敗・証拠欠落は0件でした。
最後の結果はsequence173、compute151.443ms、age上限194.728msです。

その後の未完WebSocket画像では、TCPを1436bytesずつ4回受信した後、約503ms追加の受信進捗が
ありませんでした。この試験の公開欠落は到着後の推論遅延ではなく受信の停滞と対応していますが、
Wi-FiとESP送信側のどちらが原因かを分けるpacket captureはありません。
最高温度59.887℃、throttle全49記録0、peak RSS48.910MiBでした。
選択した証拠16ファイル計56,967bytes（うちnative report31,239bytes）を保全し、元FW・Python/unit・
eco ACK/hold/ready・guard cleanupへの復元も確認しました。

[Issue #592](https://github.com/1222-takeshi/server_for_esp32cam/issues/592)では、認識継続と条件成立後の
自動復帰を別方針として検証します。ここでのstrict失敗はその成功で置き換えません。

## 条件と限界

モデル/ORT/FP32・320・threads3・score0.5/NMS0.7、QVGA/JPEG12、
固定165msまたは明示的な適応予約を使い、全10秒窓50件・age/gap500msを維持しました。推論はPiのみ、物理モータ制御は行っていません。
画像を保存せず、フレームの時刻等をRAMに保持し、試験終了時に1つのJSONへ出力します。

最初の10分指定試験は約31回の監視記録後に保護停止しており、その除外原因は未確定です。
追加診断は除外後の記録だけで、無線環境などの変動も統制していません。
今回の成功は速度改善の因果証明・ライブGT精度・24h安定性・10Hz・常設Rust移行の受入にはなりません。
