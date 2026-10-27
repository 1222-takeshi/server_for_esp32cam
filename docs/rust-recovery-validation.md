# Piでの認識継続・自動復帰の有限検証

Issue #592では、500ms鮮度保護によるhold中もtracking撮影・認識を継続し、
新ACK・有効な時計証拠・10秒50件・資源許可後の自動復帰を検証します。
[条件・結果・数値rawのSHA](rust-recovery-validation.json)を併記しています。
従来方式の[長時間試験の結果](rust-duration-validation.md)も保持しています。
時計再取得候補では10分→元環境復元→1時間を完了し、平均5Hz・全公開結果の撮影鮮度500ms以内・
全holdの自動復帰という有限検証を通過しました。holdの頻度と時間は引き続き改善対象です。

## 旧candidateの10分の結果

600秒3151件、平均5.2517Hz、全60個の10秒窓は50〜53件でした。
公開結果の撮影age上限はP95 213.897ms・最大320.078ms、compute P95は162.366msです。
最大公開間隔は695.590msでした。

注入した1回と自然発生2回の計3回でholdし、すべて新世代・新ACKから自動復帰しました。
各holdは10.354〜10.397秒、累積31.146968秒です。注入は計測開始30秒後に行い、
復帰完了と復帰後の新世代sampleが計測内にあることも確認しました。
hold中は古い結果を出さず、撮影・認識はtrackingのまま継続します。
hold統計のscopeはwarmup/終了待ちを含む`full_tracking_phase`です。

受信3217件中3216件を採用し、事前除外1件、overwrite/busy・ACK拒否は0件でした。
workerは3214件を処理・3211件を公開、破棄5件、推論失敗・証拠欠落は0件です。
最高温度73.523℃、全636監視記録のthrottleは0、native peak RSSは48.918MiBでした。

`continuous_tracking_gate_passed=true`ですが、従来の`finite_live_gate_passed`はfalseです。
公開間隔が500msを超えたため`strict_rate_gate_passed=false`、世代更新があったため
`strict_single_generation_gate_passed=false`をそのまま記録しています。
全公開結果の撮影鮮度500ms以内という保護は維持しています。

選択した証拠16ファイルの合計は555,208bytes（native reportを含む）です。
全ファイルのSHAと容量、全窓・age/gap・順序・世代・hold期間を別のPython処理で再計算しました。
元FWへの2段階復旧、元Python/unit、eco active/ACK/hold/readyとguard cleanupまで確認しています。

## 時計再取得候補の10分・1時間の結果

最初の1時間指定試験は767回の資源監視記録後に`clock_timeout`で終了しました。
推論失敗・証拠欠落は0件でしたが、時計応答の1秒期限を越えるとセッションを終了する経路が
残っていました。失敗rawを保全し、元FW/サービス・eco ACK/hold/ready・guard cleanupへ復元しました。
[PR #597](https://github.com/1222-takeshi/server_for_esp32cam/pull/597)で、continuous時のみ期限切れ時計を
破棄して新証拠を取得する処理を追加しました。新candidateの10分再試験は3149件・5.2483Hz、全60窓50〜53件で完了しました。
4回のhold（注入1・自然3）はすべて復帰し、累積41.669075秒です。撮影age上限はP95 227.295ms・
最大322.544ms、compute P95 164.374ms、最大公開間隔714.855msでした。
時計再取得0回のため、実機でのclock timeout再取得動作の証明にはしていません。
元FW/サービスとecoへの復元、選択した証拠16ファイル計556,041bytesの保全を確認しました。

同じ候補の3600秒試験は18,851件、平均5.2364Hzで完了しました。
全360個の10秒窓は49〜53件で、50件未満の窓が4個あります。
撮影age上限はP95 238.524ms・最大373.288ms、compute P95は176.454ms、
最大公開間隔は749.472msでした。全公開結果の撮影鮮度500ms以内を維持しましたが、
従来のstrict rate・単一世代・finite live gateはすべてfalseのままです。

38回のhold（注入1・自然37）はすべて新世代・新ACKから自動復帰しました。
各holdは10.387〜15.123秒、累積405.266074秒です。統計のscopeは`full_tracking_phase`ですが、
今回は全holdが計測内に収まり、計測時間の約11.26%でholdしています。
注入の復帰完了と復帰後の新世代sampleも計測内です。認識は続いていてもモーターを
連続駆動できる結果ではなく、#573で遅延とhold頻度を改善する余地があります。

受信18,986件中18,965件を採用し、事前除外21件、overwrite/busy・ACK拒否は0件でした。
workerは18,937件を処理・18,910件を公開、破棄55件、推論失敗・証拠欠落は0件です。
最高温度79.367℃、全3,597監視記録のthrottleは0、native peak RSSは50.867MiBでした。
時計再取得・遅延Pong破棄はともに0回で、実機の時計timeout回復を発火させた結果ではありません。
この経路は遅延Pongを使う実socketテストで検証しており、今回の成功をその修正だけの効果と断定しません。

元FWへの2段階復旧、元Python/unit、eco active/ACK/hold/ready、guard cleanupとrunnerの正常終了を確認しました。
選択証拠16ファイルは計3,128,192bytes（native report 2,705,046bytesを含む）です。
全SHA/容量、全sampleの順序・窓・age/gap・世代・各holdを独立再計算しました。

## 条件と検証範囲

推論はRaspberry Pi 4で行い、YOLO11n ONNX/FP32・入力320・threads3・score0.5/NMS0.7、
QVGA/JPEG12を維持しました。予約は直近2秒・最大32件のcompute最大値+25%、165〜400msです。
時計の200ppm、最大RTT1秒、証拠有効10秒、tracking ping 50msと公開鮮度500msも同じです。
各holdは実開始から30秒、最大128回に制限し、各回の開始/復帰時刻を数値として保持します。

guardianは旧候補V3でhost/Pi各70件のmockテスト・read-onlyレビュー3名、
時計再取得候補でhost/Pi各78件・レビュー2名を通過しました。期限・容量上限、
異常時の元FW復旧、鍵の子processへの除外、各holdと計測内復帰を検証しています。
unit復元はguardianで保存原本との完全一致を確認し、選択証拠にはunit本文や鍵を含めません。
nativeの認識中は画像を保存せず、数値sampleはRAM上で上限を設け、終了時に16MiB以内のJSONへ出力します。
stderr/vitalsは各2MiBの監視上限、processには16MiBのファイル上限を設定しています。

認識のモデル・精度条件は変更していませんが、ライブGT精度の再評価は行っていません。
物理モーターは動かさず、10Hz・24h安定性・常設Rust移行の受入にもしていません。
試験後は元FWと元Pythonサービスのecoへ復元します。既定の手動復帰方式も維持しています。
