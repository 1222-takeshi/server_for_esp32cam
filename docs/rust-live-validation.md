# Rust有限ライブ検証（2026-10-24〜25 UTC）

Issue #558の目的は、オフライン推論PoCを実カメラ受信・時計・ACK・モード・推論へ接続し、
Pi上で有限試験と元運用への復旧を確認することです。Macはcross buildに使用し、推論はPiで行いました。
機械可読の条件・各試験・rawのSHAは[rust-live-validation.json](rust-live-validation.json)に記録しています。
JPEG・秘密設定・大きなrawはGitへ含めず、private領域に0400で保全しました。

## 結果と限界

| 試験（UTC） | 結果 |
| --- | --- |
| 2026-10-24 14:30 / `46cdbf48` | frames_expired、60秒評価未完了 |
| 2026-10-24 14:39 / `b84c2957` | frames_expired、60秒評価未完了 |
| 2026-10-24 14:44 / `9eb13c8e` | frames_expired、60秒評価未完了 |
| 2026-10-25 00:21 / `74dd8598` | eco→tracking 60秒評価→eco完了 |

成功回は60秒316件、**5.2667 Hz**。各10秒窓は **53 / 53 / 52 / 53 / 53 / 52件**でした。
最大公開gap401.165ms、保守的capture age上限のP95は225.429ms・最大406.969ms、
computeのP95は161.904msです。全評価sampleのboot/世代/順序/鮮度と各窓を別のPython処理でも再計算しました。
モード変更中に完了した旧tracking世代1件は破棄し、評価区間外として記録しています。
試験全体の最大温度63.783℃、throttle常時0、peak RSS48.965MiB。CPU使用率や消費電力Wの比較は行っていません。

**4回中1回の有限試験完了です。継続5Hzの安定性、原因解消、常設本番移行の受入ではありません。**
失敗回では、到着age上限366.408msの画像を165ms推論予約gateで破棄し、
次の通常推論152.121msが完了する時点では公開gap521.601msとなってholdへ保護しました。
受信からdropまでの経過は0.117ms、device capture/send失敗・receiver overwrite/busy・推論失敗の増分は0でした。
旧失敗rawにはcapture→send経過と到着age下限がなく、device内滞留・送信後遅延・時計不確実性の
寄与は未確定です。PR #571/#572で32件の診断を追加しましたが、過去rawへの補完は行いません。
異常の切分けと再現性は[Issue #573](https://github.com/1222-takeshi/server_for_esp32cam/issues/573)で追跡します。
別時刻の試験で無線環境を統制していないため、成功を診断追加による速度改善とは解釈しません。

## 維持した条件と復旧

#553で固定画像の互換性を確認したモデル・ORT・JPEG decoder・FP32/320/threads3・score0.5/NMS0.7を
維持し、165ms予約・500ms鮮度/gapを緩和していません。今回のlive画像のGT精度は評価していません。
10Hz・24h安定性・W・UI/保存/通知の全面Rust移行は今後の受入です。物理モータ制御は行っていません。

native stdout/stderrはOS側で各2MiB上限、待機は最新1枚、sample8192件・破棄診断32件をRAMに保持します。
成功回のreportは56,877bytes、vitalsは11,424bytesで、画像の保存やframe単位disk logを追加していません。
永続復旧guard・共有deploy lock・期限付きsystemd runner・監視ticketで有限試験を監督しました。
全4回ともcamera bootを維持し、元Python source `613d293` のeco active/ACK/hold/health readyへ復旧。
元guard有効・trial guard無効・4回分のcleanup完了を実機snapshotと追加manifestでも保全しました。
FW OTA・本番unit/source変更・DB巻戻しはありません。

## Tests / 静的解析 / Review

最終binaryはPi ARM64 native library61件+CLI1件、復旧helperはPi/Mac各17件成功。
clock51・mode18のPython参照trace、fmt、全feature/全target Clippy -D warnings、cargo audit、
ローカルARM64 act、GitHub CIを通過しました。変更ごとに2名以上read-onlyレビューしB/Iを0件に解消。
復旧helperは3名レビューしました。有限試験の成功と残る安定性課題を分けて記録しています。
