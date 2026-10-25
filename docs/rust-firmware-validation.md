# ESP32送信候補の有限Pi検証（2026-10-25 UTC）

Issue #573の断続的な到着遅延を調べるため、PR #575の受信診断とPR #577の送信候補を使い、
Raspberry Pi 4上で60秒試験を3回実施しました。Macはbuild・集計・mock testに使用し、推論はPiです。
条件・測定値・rawのSHAは[rust-firmware-validation.json](rust-firmware-validation.json)に記録しています。

## 結果と判断

| 試験 | 件数 / 60秒 | 認識Hz | 最大公開gap ms | age P95 / 最大 ms | compute P95 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| 候補 B1 | 316 | 5.2667 | 285.348 | 215.116 / 301.654 | 165.430 |
| 候補 B2 | 316 | 5.2667 | 315.381 | 248.053 / 363.504 | 169.113 |
| 候補 B3 | 316 | 5.2667 | 279.639 | 209.880 / 291.832 | 160.072 |
| 元FWの事後reference A1 | 316 | 5.2667 | 306.494 | 217.225 / 333.195 | 158.533 |

各回の10秒窓は全て **53 / 53 / 52 / 53 / 53 / 52件**。3回は別々の60秒枠で、
間にeco復帰とPython再起動を挟み、候補camera bootは同一です。連続3分の試験とは扱いません。
全sampleのboot/世代/順序/age・各窓・gap・P95を独立したPython処理でも再計算しました。
各回のworker drop 1件は最終eco変更後に完了した旧tracking世代で、評価区間外です。
推論失敗・evidence loss・receiver drop/overwrite/busyは0、device capture/send失敗の増分も0でした。

**事前に定めた「候補3回全てが有限5Hz gateを通過」は達成しました。**
ただし元FWのA1も通過し、無線環境を統制していません。候補による改善効果・断続遅延の
原因解消は断定せず、#573は継続します。開始前reference A0は元loggerの停止コード137で
Rust起動前に中止しました。性能失敗や有効なbaselineに数えず、rawと復旧結果を保全しています。
以前のclock200ms/QUICKACKの失敗も保全し、不採用のままです。

## 条件と保存量

候補はECF込み1400byte以上で14byteのheadroomを使い、WS headerとECF/JPEGを同じlibrary writeへ
渡します。小frameは従来経路を維持します。1writeを1TCP segmentや再送解消とは扱いません。
Espressif32 6.5.0 / WebSockets 2.7.3、QVGA/JPEG12、モデル/ORT/decoder・FP32/320/threads3・
score0.5/NMS0.7、clock50ms、165ms予約・500ms鮮度/gapを維持しました。liveのGT精度は未評価です。

3回の全native phaseを含む最大温度は65.731℃、throttle常時0、peak RSSは48.969MiBでした。
report計183,708bytes、vitals計34,272bytesで、画像保存やframe単位disk logは追加していません。
stdout/stderrは各2MiB上限です。FW/実行fileと他のguard metadataはこのbyte集計に含みません。
raw・FWはprivate領域の0400 fileで保全し、秘密設定もGitへ含めていません。

## 復旧と残る課題

ユーザー承認後、候補FWを一時OTAし、試験後は元profile FWへ転送しました。
APIに依存しない復旧転送→現物boot anchor→確認転送→別bootとeco ACKの2段階で検証しました。
SHAは転送imageの値であり、device flashを直接読戻したattestationではありません。
元Python source `613d293` とunitを維持し、eco active/ACK/hold/health readyへ復帰済みです。
旧guard有効・一時FW/Rust guard無効・cleanup完了も実機snapshotで保全しました。
DB巻戻し・物理モータ操作・常設FW切替は行っていません。

確認用OTA付近でrunnerがexit1となり、永続guardは3回失敗後の再試行で自動復旧を完了しました。
転送stageのmarkerで完了済みstageを区別しています。OTA error textは残っておらず原因は未確定です。
固定host port32320の再使用待ちと整合する時間差はありますが、推測として
[#578](https://github.com/1222-takeshi/server_for_esp32cam/issues/578)で診断・改善を追跡します。
既存のmask問題[#576](https://github.com/1222-takeshi/server_for_esp32cam/issues/576)も未解決です。
10Hz・長時間安定性・環境変化時のGT精度・消費電力W・全面Rust移行は別の受入です。

## Tests / Validation

FW guardはMac/Pi各42件、read-only mountのARM64ローカルactでも42件成功しました。
電源断・排他・API不通・転送後の検証失敗・古いbootの誤認・子process回収をmockで検証し、
3名read-only reviewのB/Iを0件に解消しました。Ruffも成功しています。
FW payload互換性・13関連tests・3target build・CIはPR #577の検証を維持します。
本記録は全4件の有効reportの独立再計算とJSON整合・差分checkで検証しています。
