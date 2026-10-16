# 高頻度撮影・猫認識の開発計画

2026-10-16のユーザー合意を記録する。計画Issueは[#484](https://github.com/1222-takeshi/server_for_esp32cam/issues/484)、全体管理は[#451](https://github.com/1222-takeshi/server_for_esp32cam/issues/451)。本書は未実装の計画で、本番・ファーム・モデル・モータの挙動を変更しない。

## 目標と計数

| 段階 | 新規画像の撮影 | 新規画像に対する猫検知推論の完了 | 状態 |
|---|---|---|---|
| 最低要件 | 5Hz以上、200ms周期が目安 | 毎秒5件以上 | 未達 |
| 希望目標 | 10Hz以上、100ms周期が目安 | 毎秒10件以上 | 未達 |

最初の受入は1台を対象にする。カメラごとに撮影・受信・推論開始・推論完了の実測Hzを分ける。同じ画像の再推論、再送、追跡の位置補間、画面再描画は新規画像の認識へ加算しない。猫なしという正常な推論結果も1件と数え、推論失敗・意図的skipは成功数へ加算しない。撮影10Hz・認識5Hzは最低要件の候補であり、10Hz認識の達成ではない。

Hzは処理件数、撮影→認識結果の遅延は情報の古さを示す別指標である。200ms/100msは周期の目安であり、自動的にend-to-end遅延の合格上限にはならない。許容遅延と鮮度TTLは猫の動き・モータ機材・停止条件から駆動前に固定する。猫のprecision/recall、誤通知/時間も速度と同時に評価する。

## 現状と測定根拠

- 運用は約5秒間隔。遠隔設定の`capture_interval_seconds`は整数5〜86400に限定され、サーバーとファーム双方の変更が必要（[API契約](remote-control-api.md)、[サーバー検証](../../script/routes.py)、[ファーム](../../firmware/esp32cam-http-upload/src/main.cpp)）。短周期設定を追加する際は既存秒単位設定との優先順位・互換性・ACKを定義する。
- [WebSocket受信](../../script/websocket_routes.py)の既定最小間隔は1秒。受信した画像をファイル保存・SQLite記録してから推論へ入れる。[推論サービス](../../script/inference.py)はプロセス内の単一detect呼び出しと上限なしFIFOを使う。単に撮影周期を下げると欠落・拒否・待ち行列増加があり得る。
- ファームはJPEG・1フレームバッファで、撮影と送信を同じloopで行う。同期的な設定pollや、LED使用時の150msの既定warmupも短周期の制約になる。夜間は露光時間と画質を含めて実測する。
- [2026-10-14の測定記録](https://github.com/1222-takeshi/server_for_esp32cam/issues/451#issuecomment-6171248121)はPi 4、1 CPU thread、各入力条件20画像×3回。SSD/LiteRT人物検知300入力はmedian/P95が137.1/137.5ms、公式YOLO11n ONNX 320は276.6/284.8ms、640は1118.7/1129.1msだった。
- YOLOは公式baseのFP32 ONNXと独自前後処理による速度proxyで、007の正式な組込みや撮影/転送/保存/待ちを含む実運用測定ではない。SSDはperson-onlyでタスクも異なる。モデル単体の時間の逆数を運用Hzへ置き換えない。公開画像の精度比較も本人猫・消灯環境の受入を代替しない。

## 独立Issueへ分ける実装順序

| 段階 / 所有領域 | 実装・検証 | 次へ進む条件 |
|---|---|---|
| 1 撮影・転送 / デバイス・受信計測 | 200ms/100msの周期設定、JPEG連続撮影候補、撮影と設定取得の分離、frame IDとboot/session ID。5/10Hzを受けられるレート上限を明示した認証付き隔離受信器で、推論・永続保存を外した上限付き試験を行う。JPEG検証・サイズ/接続上限を維持し、本番の1秒制限を経由せず撮影/転送Hz・bytes・欠落を測る | 5/10Hzそれぞれの撮影・転送実績、Wi-Fi/露光/メモリの制約が分かる。未達は記録して構成を再検討 |
| 2 鮮度と処理経路 / サーバー | WebSocket受信制限の短周期対応を明示し、信頼できる撮影時刻または遅延の上限評価、結果時刻を追加。メモリ上推論と非同期保存を分離し、カメラごとの最新待機1枚＋実行中1枚を基本案にする。認証・JPEG検証・サイズ/接続上限を維持する | 保存/通知の遅さで推論が滞留せず、drop・順序逆転・再接続・時計異常を観測できる |
| 3 猫認識5Hz / 推論 | 基準と比較候補のruntime互換性・出力等価性を確認。CPU 1/2/4 thread、入力形状/サイズ、INT8量子化を同条件で比較。重いネイティブ処理はrequest greenlet外で行う | 事前固定した精度・遅延・負荷条件で30分の5Hz受入を満たす。量子化や解像度変更後も再評価 |
| 4 猫認識10Hz / 推論・運用 | Pi 4最適化で不足する場合、既存Mac/DGX等への推論切り出しや別ハードウェアを比較候補にする | ネットワーク遅延・通信断・資源/運用負担を含めて30分の10Hz受入を別判定する。機材・購入は未決定 |
| 5 現場受入・角度推定 / 評価・制御 | 本人猫/猫なし・昼/消灯の別セッションGTと連続映像で評価。モータなしで角度推定・鮮度棄却・停止判断を検証 | [猫追従契約](cat-following-capability.md)の校正・機材・停止条件を含む受入を満たしてから別Issueで実機駆動へ進む |

撮影試験は1→2、結合した猫認識は1/2→3→4を基本の依存順序とする。夜間撮像方式・現場GTの準備は並行でき、5の受入には3と既存追従契約を必要とする。10Hz未達でも5Hz受入を別に記録できる。複数カメラの台数別負荷とモータ指令周期は後続Issueで定義する。

最新画像優先で上書きした待機画像はdropとして計上する。高速化の代わりにskipや補間を増やして達成扱いにしない。1つのInterpreterへ並列にinvokeしない。複数実行器が必要なら独立インスタンスとカメラ内の結果順序を設計する。保存はイベント/間引き等の方針・保持上限を決め、保存待ちも無制限に蓄積しない。高頻度の全画像永続化や再学習・閾値変更を暗黙に導入しない。

最適化候補の根拠は[ONNX Runtime thread設定](https://onnxruntime.ai/docs/performance/tune-performance/threading.html)、[量子化](https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html)、[ESP32 JPEG連続撮影](https://github.com/espressif/esp32-camera#important-to-remember)。いずれも本構成の達成保証ではなく、量子化が必ず速くなるとは限らない。

`script.onnx_detector.OnnxYoloDetector` は公式 YOLO11n FP32 ONNX の 320px・80 COCO class export に対応する猫検出コンポーネント。利用時は `.[onnx]` をインストールし、モデルファイルを Git 外に置く。`OnnxYoloDetector(model_path, threshold=0.5, num_threads=4).detect(jpeg_bytes)` は EXIF を反映した原画像の正規化 xyxy を `Detection(label="cat")` で返す。モデルの metadata があれば class 15 が `cat` か確認する。前後処理の CPU 時間もあるため、サーバーから呼ぶ場合は `InferenceService` と同方式の専用 single-thread pool を通す。独立した公開training20枚の互換性比較では、検出器と参照NMSの両方をconfidence 0.5に固定した（参照ベンチ単体の既定は0.25）。IoU 0.7、NMS 最大300件は参照ベンチと同じ。対象カメラでの精度・負荷を保証しない。

## 受入試験と記録

試験開始前にモデル/labels/画像・GT/前後処理/runtime/依存関係のSHAまたはversion、入力寸法、threshold、thread数、機材・照明、ウォームアップ、30分の計測窓を固定する。許容する欠落率・短時間の周期低下・最大結果間隔・遅延P95・鮮度TTL・CPU/温度/RSS・既存API応答・精度の合格上限/下限も先に記録する。未決の条件があれば受入完了としない。画像・元GT・baselineは不変とし、結果を見てsilent tuningしない。

| 指標・試験 | 記録・判定 |
|---|---|
| 新規性とHz | camera/boot/frame IDで重複を除外し、撮影/受信/推論開始/正常完了数を記録。30分全体と10秒窓のHz、最大結果間隔を示し、事前の短窓低下条件も検証 |
| 欠落と失敗 | 撮影→受信欠落、受信→推論の上書きdrop、推論失敗/skip、保存drop、再送を区別。分母と区間端の実行中フレームを記録する |
| 鮮度 | capture→receive→queue→preprocess→invoke→postprocess→resultの区間、capture→resultのP50/P95/最大。時計offset/誤差上限、boot変更を記録し、不明な撮影時刻を受信時刻で代用して合格させない |
| 精度・暗所 | 本人猫/猫なし、昼/消灯を分けたprecision/recall・誤通知/時間、画質棄却と見逃し。露光・動体ブレを記録し、高Hzのための減光/低解像度での劣化を確認 |
| 同時負荷 | 現行サービスのupload、閲覧、health/ready、保存・通知と同時にCPU/温度/RSS・API遅延を測定。30分での温度上昇・throttling・キュー増加を確認 |
| 境界・異常・回帰 | 最低/希望周期、過負荷、不正な周期/認証/JPEG/サイズ、重複/順序逆転、通信断/再接続/再起動/時計ずれ/古いフレーム、保存/推論失敗、暗所と対象消失を試験。従来の5秒設定/ACK/OTAとHTTP/WS uploadを回帰確認 |

古いフレーム・対象消失・品質不足・通信/推論異常では角度候補をholdし、既存の停止契約を維持する。撮影Hz、猫認識Hz、追跡更新Hz、モータ指令Hzは個別に報告し、認識速度の合格を実機追従の完了とは扱わない。

## 実装時の手順と成果物

各段階に新しいopen Issueを作成し、origin/main同期後に専用`feat/`または`fix/`branchとworktreeを作る。writerはworktreeごとに1名。normal/edge/invalid/regressionの意味あるテストを追加・実行し、GitHub Actionsの関連workflowを確認・必要時修正、`act`等でローカル検証、既存静的解析と`git diff --check`を行う。commit・remote push・Issue連携PR作成後に2〜4名のread-onlyレビューでBLOCKING/IMPORTANTを解消し、Quality Gate後にsquash merge、remote branch削除、main同期とclean確認を行う。

再現に必要な小さなconfig/manifestは追跡し、大きな画像・モデル・ログ・計測CSVはGit外へ保存する。新しい生成物ディレクトリをリポジトリ内で使う場合は`.gitignore`へ追加する。各PRにTests/Validation、固定入力、測定条件と未達事項を記録する。
