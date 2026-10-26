# Rust frame workers

`frame-worker`はIssue #553のオフライン互換・性能検証用です。ECF2をstdinから有限回再生し、認識結果と計測値を
JSON Linesでstdoutへ返します。実行中は1画像・待ちqueueなし、最大120画像・出力1行64KiBです。
不正/途中EOF/重複/逆順/モデル故障/出力故障で非0終了し、無限retryしません。

`frame-worker`は本番Pythonサービスへ接続せず、WebSocket認証、時計同期、profile ACK、実時間の500ms鮮度、
eco/tracking制御、保存/通知を提供しない段階です。全結果に`replay_only=true`、
`freshness_checked=false`、`actuation_state=hold`を強制します。実機の新鮮な認識結果や
motor commandへ流用してはいけません。オフラインHzをcamera live Hz/10Hz達成とは扱いません。

`frame-live`はIssue #558の有限Pi実機検証用で、下記の認証・時計・ACK・worker・試験制御を
接続します。通常のPython UI/保存/通知の置換は含まず、外部の監視・自動復旧が必要です。

## 入力・推論契約

libraryはECF1/ECF2を検証しますが、CLIはECF2必須です。big-endian header、正整数identity、
capture/sendの順序、世代上限2**53-1、JPEG128KiB、宣言長を検証します。宣言長はallocation前に
検査し、retired bootは直近32件を記憶します。CLIはACK/時刻の信頼性を検証しません。

JPEGは幅320・高さ240または320、最初のscan前のEXIFを拒否（後続scan間metadataは検査しない）、
scan上限64。decode前に寸法を検査しRGB bufferを制限します。resizeせずpadding114/CHW/FP32へ
変換します。純Rust decoder2種類では固定20画像が不一致だったため、同じupstream版
libjpeg-turbo3.1.4.1の公式static archiveをsafe Rust wrapper経由で使います。
Pi上の固定20枚ではRGB/tensor全bytes一致を確認しました。元の不一致結果も保全します。

モデルSHAは`ff1013da8734fe2b8ede5f34a2124855d447fbefd248d148e9f29e7f8e28c9c4`に固定。
PiのARM64 ORT1.23.2 library SHAも検査し、canonicalな同一絶対pathをhash/loadへ渡します。
libraryは管理者専用・初期化中は不変とします。モデルは検証した同じbytesからsessionを生成し、
ORT/modelを自動downloadしません。CPU/FP32/320/3 intra threads/1 inter thread/sequential/
spinning off/ORT_ENABLE_ALL、score0.5/class-aware NMS0.7/全class合計max_det300です。
同scoreの候補はNumPyのtie順位をまだ保証できないため、PoCでは結果を推測せず拒否します。

## Build・再生

Rust 1.95.0で検証します。次はLinux ARM64 build環境向けの例です。
Macからのcross buildでは別途ARM64 targetとlinker設定が必要です。Pi評価時はcross buildした
binaryを転送し、PiへRust toolchainを常設していません。

```sh
prefix=$(mktemp -d)
python3 scripts/prepare_rust_jpeg.py --arch arm64 --output "$prefix/jpeg"
export TURBOJPEG_SOURCE=explicit TURBOJPEG_STATIC=1 TURBOJPEG_LIB_DIR="$prefix/jpeg"
cargo fmt --manifest-path rust/frame-worker/Cargo.toml --check
cargo clippy --manifest-path rust/frame-worker/Cargo.toml --locked --all-targets --all-features -- -D warnings
cargo test --manifest-path rust/frame-worker/Cargo.toml --locked --all-features
cargo build --manifest-path rust/frame-worker/Cargo.toml --locked --release --features jpeg
rust/frame-worker/target/release/frame-worker /private/yolo11n.onnx /private/libonnxruntime.so.1.23.2 \
  < /private/input.ecf2 > "$prefix/results.jsonl"
```

取得toolは公式Deb SHAを検証し、path/linkを展開せずstatic libraryとLICENSEだけを新規700
prefixへ出力します。download8MiB/file16MiB/package計64MiB・1024members以下、manifestを最後に
作成します。crate付属の旧codecはbuildしません。本番Pillow/OS packageを変更しません。
binary配布時は生成したLICENSE.mdも同梱し、受入証跡は保護したprivate領域に保存してください。
Cargo.lockは追跡し、build成果物は既存`.gitignore`の`target/`で除外します。

## 計測値の解釈

`decode_us/prepare_us/infer_us/post_us/total_us`はenvelope受信後からNMS完了までのcomputeで、
SHA/JSON/入出力を除きます。infer_usはvalidation/copyを含むDetector APIの時間で、
純kernel時間ではありません。各言語のAPI境界が違うため、主比較にはtotal_usを使います。`wall_us`はmodel初期化後の再生時間で、それらのoverheadも含みます。
Linuxの`usage.cpu_ticks`はprocess全体のuser+system ticks（単位はOSのCLK_TCK）、
`rss_kib/hwm_kib`はprocess RSS/初期化込みpeakです。不明ならnullとし、0と推定しません。
固定画像の前処理/検出互換は、現場GTによる精度、24h安定性、消費電力W、本番移行と別の受入です。

Clock port (#558): `clock::ClockWindow` preserves the Python eight-sample, 10s
expiry and 200ppm drift interval. Run `python3 scripts/check_rust_clock.py` to
verify the checked-in differential fixtures. This is a library gate only;
a valid clock interval alone establishes neither profile ACK nor publication
freshness, and the offline replay still does not perform live freshness checks.

`operating_mode::OperatingMode`はACK・世代・eco/tracking watchdogの移植libraryです。
全操作とRAM内の公開callbackを呼出側の同じMutexで直列化し、信頼できる単調増加us clockと
CSPRNGの初期世代を渡す必要があります。公開callbackは非blocking・有限で、失敗/panicで
publication_failedへ保護遷移します。時計によるcapture age≤500ms判定は別途必須です。
5秒ACK期限、trackingの500ms gapと連続10秒窓≥50件、ecoの17秒猶予と正常3件での復帰を
Pythonと同じに保ちます。tracking故障後やresource故障は自動解除せず、再接続はecoです。
`python3 scripts/check_rust_mode.py`とRust testsで18本のPython参照traceを照合します。
このモードlibrary単独ではモータ制御・live Hz達成を意味せず、実測はframe-liveと復旧監視で行います。

任意feature `transport`の`Connection`はcamera path/API keyを検証するLAN内WebSocketです。
TLSは提供せず、通常運用への常設切替は行っていません。handshakeは接続開始から2秒/8KiB、
受信frame/messageは共通でECF2+JPEG128KiBまで読み込み、textの受理上限は読み込み後に512Bで検査します。
receive呼出は分割frameの継続中も絶対期限50msで戻り、writeは200msです。
key重複・query付きpath・未mask frame・圧縮を許可しません。keyを含むrequestやerrorを表示せず、
固定長SHA256 digestをconstant-time比較します。nonceと初期世代はOS CSPRNGから生成します。
上位ownerは1camera session所有、hello/clock/全体の期限、message rate、frame freshnessを
別途検証し、errorでConnectionをdropしてください。この層だけで画像の公開を許可しません。

`control`はfirmwareのhello/ACK/pong/statusを512B以内の厳密なJSON schemaとして検査します。
SessionClockはboot固定・同時pending1件・1秒RTT/nonce照合・eco2秒/tracking50msのping間隔を
管理します。Arrivalは受信時の保守的age区間を固定し、待ち時間と推論予約時間を加算して
500ms以内かを判定します。新しいclock sampleによって待機画像の期限を延長しません。
このlibraryだけではACK/世代/順序/JPEG/connection所有権を確認できず、公開許可には使えません。

Arrivalはboot/sequence/capture/send/世代の識別子に結び付き、別のフレーム識別子への転用を拒否します。JPEG本体の照合は含みません。
公開gateにはMode自身の判定時刻を渡し、watchdog・鮮度判定・公開stampを同じ時刻で
記録します。gate後の遅延によって古い結果の期限を更新しません。

`pipeline::Worker`はnative推論を専用threadで実行し、待機は常に最新1枚です。前段で165msを
予約し、完了時はモードと同じMutex下で500ms/世代/キャンセルを確認します。公開・保存用に
元画像を複製せず、最新metadata/detectionと有限試験の最大8192件のRAM内sampleだけを持ちます。
sample上限到達はevidence_lostへ計上し、SDへframe単位logを書きません。
JSON/clock/順序/JPEG検査とsession所有権は上位ownerの責務です。native推論そのものはthreadから
強制中断できないため、有限試験にはprocess全体を終了できる外部supervisorの期限が必須です。
Worker.stopは実行中native呼び出しの終了を待ちます。Model/runtime実体と通常運用配線は別段階です。

`session::Receiver`は上記libraryを接続する有限の1camera coordinatorです。State内のleaseで
同時接続を拒否し、所有tokenだけが切断処理を行います。hello5秒、clock1秒、session最大600秒、
最大20000messages/7000frames、1秒固定窓100messagesを上限にできます。ECF2/ACK/boot/順序/ageを
確認し、native推論はworkerへ渡します。controller callbackは同じmode mutex内で呼ぶため、
有限のRAM操作のみ許可します。JPEG decode/model/TLS/listener/supervisorはこの層に含みません。
終了時はleaseを解放する前にTCP接続を閉じます。final_statusは切断処理前のsnapshotで物理制御を許可しません。

`trial::Trial`はeco active確認→tracking warmup→10秒単位の有限評価→eco active確認を行います。
評価はactive確認後の新しい公開結果を基点とし、区間内の全sampleを同boot/世代か検証して各10秒窓50件以上、端点を含む最大gap500ms、全sample age≤500msを要求します。
故障時にtrackingを再要求せず、raw sampleを調整して成功扱いにしません。supervisorの準備済み
信号が必須です。heartbeat ticketはnonce/128B/時刻誤差1.5秒を検証し、cameraの単調時計とは別です。
このlibraryはlistener/model/復旧機構を起動せず、実環境精度/10Hz/24h/Wの受入ではありません。

`frame-live MODEL ORT ADDRESS CAMERA`は`jpeg,transport`両featureが必要な有限Pi試験binaryです。
per-device key mapが非空ならcamera専用keyを必須とし、shared keyは明示したdevice IDとの完全一致を
要求します。実keyは環境から読み、argv/log/reportへ出しません。EXPECTED_BOOTと16hexのtrial nonceを
別のprivate環境変数で渡し、stdinから`ready NONCE UNIX_MS`の監視ticketを定期的に送る必要があります。
監視readerは1行128B、cached ticketはmonotonic2秒/wall差1.5秒以内のみ有効、EOFで停止します。
model初期化後のcamera接続期限20秒、session180秒、tracking評価60秒、出力report最大2MiBです。
reportはJPEGを含まず、測定失敗コードを保持し、global_acceptance/physical_motor_controlをfalseに
固定します。native終了待ちを含むため、外部のprocess deadlineと永続的な復旧guardを必ず併用してください。
通常のPython service/UI/保存/通知はこのbinaryへ移行しておらず、本番切替は別のguarded手順です。

有限live reportには直近32件だけのworker drop metadata（段階、identity、保守的age、compute時間）
を含みます。JPEG・key・frameごとのdisk logは追加せず、500ms/165ms予約などのgateは維持します。
到着時の保守的ageと到着後の経過時間も記録し、推論前後・キャンセル等の段階を区別します。
到着後経過はqueue待ちとcompute等を含む値で、純queue時間ではありません。dropを成功扱いにしません。
このringは推論threadで破棄した結果の診断で、全dropの台帳ではありません。queue置換・busyは
receiverの集計で確認し、停止時に未処理のまま残るqueue itemはringに含みません。

破棄metadataの`capture_to_send_us`はdevice上のcapture→send経過、`arrival_age_low_us`/
`arrival_age_high_us`は到着時の保守的age区間です。device内の滞留とhost到着までの遅延を
検討する材料であり、この区間だけで無線の原因を断定しません。

## 有限Pi実機検証の記録

#558の有限ライブ配線とeco→tracking→eco試験は[検証記録](rust-live-validation.md)を参照してください。
4回中1回で60秒316件（5.2667Hz）を確認しました。失敗3回も保全し、継続安定性・原因解析は#573へ継続します。

#573の受信診断はWebSocket受信直後・mode mutex取得前に、worker由来の同じ時計でstampします。
公開までの経過は既存Arrivalで加算し、500ms/165ms gateを緩和しません。clock probeも50msです。
reportはreceive/send call・admission guardの最大所要usと、直近32件のtransport I/O metadata
（read/write/message完了、byte数、所要us、wall timestamp、timeout等）を保持します。
直近32件のframe sequenceにWS完了のwall stampと、その後に採った受信mono stampを記録します。
採時点が異なり、スケジューラ待ちも含むため、両者を同時刻のclock変換対とは扱いません。
observed_unix_usはuserspaceでの観測時刻で、kernelのpacket到着時刻ではありません。
elapsed_usはread/write wrapperの経過（処理・待機・scheduler等を含む）で、message完了markerはnullです。
wall timestampは通信ヘッダー採取との照合専用で、鮮度・制御の判定には使いません。
JPEG・キー・message本文は保持せず、frame単位disk logを追加しません。call所要時間を
純粋なnetwork latencyとは呼びません。clock200ms/TCP_QUICKACK案は実機で再発し不採用です。
原因解析と改善受入は#573で継続し、#558のrawは変更しません。

送信候補の3回の有限5Hz試験と元FW復旧は[追加検証記録](rust-firmware-validation.md)を参照してください。
候補3回は全て通過しましたが、元FWの事後referenceも通過し、因果・長時間安定性は未確定です。


### Bounded longer live experiments

`frame-live` keeps the sixty-second default. The private supervisor may explicitly
set `ESP32CAM_RUST_TRIAL_SECONDS` to a multiple of ten from 10 through 3600
(e.g. 600 for ten minutes). Invalid values fail before loading the model. The
receiver budget is the requested duration plus 120 seconds for mode transitions;
it remains capped at 3720 seconds, 50000 frames and 200000 messages, with the
existing rate limit unchanged. Runtime frame/message budgets are smaller for
shorter runs.

Publication evidence is bounded at 65536 metadata records; overflow still fails
the trial. The single final JSON report is capped at 16 MiB. No JPEGs or model
outputs are added to these records. The supervisor must use matching finite
process/file limits, check disk/thermal/memory readiness throughout, and restore
the original firmware and eco mode on success or failure. Each ten-second window
still requires 50 results; maximum age and result gap remain 500 ms. Expanding
the experiment duration does not establish sustained performance, accuracy parity,
10 Hz operation, or permission for permanent rollout.

10分追試の中断と復旧、受信除外の診断は
[rust-long-trial-validation.md](rust-long-trial-validation.md)を参照してください。

### Opt-in load-aware inference reservation (#584)

For a new finite experiment only, the supervisor may set
`ESP32CAM_RUST_TRIAL_ADAPTIVE_RESERVE=1`. Missing/`0` keeps the fixed 165 ms
reservation; other values fail before loading the model. `Worker::start` also
keeps the existing fixed policy by default.

The candidate uses the largest compute duration observed in the last two seconds
(at most 32 observations), plus 25%, clamped to 165–400 ms. It discards a waiting
image before inference when arrival age + elapsed time + this estimate exceeds
500 ms. Old observations expire; backwards time cannot expire them early. This
is a conservative estimate, not a deadline guarantee. The unchanged publication
gate still rejects every result older than 500 ms.

No detector, model, thread count, precision, input/JPEG setting or mode watchdog
changes. The latest-one queue remains bounded; the estimate adds at most 32
numeric pairs in RAM. The final report states whether the candidate was enabled,
and drop metadata includes the reserve used. Acceptance still requires 50 results
in every ten-second window and all age/gap limits, first for ten minutes and then
one hour. Preserve fixed-policy and failed raw evidence; do not treat regression
simulation as live performance acceptance.

The supervisor copy must explicitly pass the flag in its child environment and
verify `adaptive_inference_reserve=true` in the resulting report. Archive its
source/configuration separately from the fixed-policy baseline before a trial.
