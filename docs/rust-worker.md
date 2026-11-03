# Rust frame workers

Issue #588でmodeライブラリに`OperatingMode::new_with_tracking_recovery(..., true)`を追加しました。
既定の`new()`は従来どおり手動復帰です。明示的に有効にした場合のみ、鮮度切れ・周期不足で
認識をtrackingのままholdにし、世代更新、新ACK、10秒50件、資源許可後に復帰します。
繰り返すgapは周期証拠だけをリセットし、ACK/世代更新を連打しません。資源・推論・公開異常、
切断、ACK期限切れ、手動ecoは従来の退避を優先します。hold回数・累積秒・復帰回数も記録します。
呼出側はState mutexと資源permitを保持し、現在の撮影鮮度を検証した間だけ
`set_recovery_ready(true)`を許可し、Stateを解放する前にfalseへ戻す必要があります。
既存18本の参照traceは変更せず、別の27本でPythonの新方針との一致を検証します。
`frame-live`は`ESP32CAM_RUST_TRIAL_AUTO_RECOVERY=1`を明示すると連続復帰用controllerを使います。
省略/0は従来のstrict試験です。`ESP32CAM_RUST_TRIAL_INJECT_HOLD=1`を併用すると、60秒以上の
実験で計測開始30秒後に一度holdを注入し、新ACKからの復帰を確認します（注入を報告に明記）。
回復待ちは1回30秒・128回まで、手動eco・terminal異常で試験を終了し、最後はactive確認後ecoへ戻します。
復帰許可はState mutexとsupervisor ticketのguardを保持した間だけ有効です。現在の撮影鮮度と
ticket期限を合成したdeadlineを復帰commitでも再確認し、workerにcached許可を残しません。
連続試験は平均5Hz以上・公開結果すべて500ms以内・全holdの復帰を必要とします。
`continuous_tracking_gate_passed`と、従来の`finite_live_gate_passed`を分け、後者を連続試験の
成功でtrueにしません。measurementの`strict_rate_gate_passed`は全10秒50件/gap500ms、
`strict_single_generation_gate_passed`は単一世代という従来指標です。hold秒数はwarmup/終了待ちを
含むtracking期間の累積値で、実機での物理モーター制御や恒久導入の受入結果ではありません。
`tracking_holds`は最大128件の`[generation, started_us, recovered_us]`で各回の30秒上限を検証します。
`injected_hold_at`は`[started_us, generation]`です。注入前後の世代の結果が測定区間内に存在することも
必要とし、復帰完了と復帰後の結果も測定区間内に要求します。
`tracking_hold_statistics_scope=full_tracking_phase`はwarmup/終了待ちを含む集計であることを明示します。
`strict_single_generation_gate_passed`は測定区間の実sample世代から計算します。
continuous試験では、pendingの時計交換が処理時に1秒を超えた場合もholdのまま新しい交換を開始します。
期限切れの時計証拠を全破棄し、旧証拠で受付済みの待機/実行中jobと最新結果をfenceします。
同じbootの既知の期限切れnonceだけを1回捨て、未知nonce・再送replay・boot変更は従来どおり終了します。
期限切れnonceは8個、再取得はsession最大128回に制限し、session/hold/ACK期限も維持します。
当該世代のACKと再取得した時計で検証した結果の10秒5Hzから復帰し、RTT1秒・証拠有効10秒・200ppm・500ms鮮度は同一です。
既定strict sessionは時計期限切れで終了します。reportの`clock_timeout_recovery_enabled`と
receiverの`clock_restarts`・`retired_pongs`・直近8件の`[sent_us, fenced_us]`で動作を確認できます。
`receiver.hold_diagnostics`はholdを初めて観測した時の数値snapshotを最大128件保持します。
理由・世代・hold開始時刻、最新結果、推論中frame、直近8件の受信/IO/推論完了metadataを含みます。
`worker_ready_us`はworkerがState内で適格性を確認した時刻で、native呼出し直前の`begun_us`と区別します。
推論完了の`finished_us`と判定後の`decided_us`からcommitまでの経過も確認できます。
共有ロック内では固定長の数値だけをコピーし、JSON化はworker終了後に行います。
待機queueの瞬間状態は直接観測せず、受信から推論開始までの時間・完了履歴・破棄記録で解析します。
`after`は観測した推論中frameが切り替わった後の最初のsnapshotです。対象がなければ次の観測です。
`after_source=worker_shutdown`は終了後の補完を示します。8件ringからの退去は
`watched_sequence`で検出し、記録がない場合に原因を推測しません。開始と観測時刻は同一とは限りません。
画像・検出座標・認証情報は含めず、上限超過や終了までに観測できなかったholdは`hold_diagnostics_lost`へ計上します。
診断は保護・世代fence・復帰条件を変更せず、安定5Hzの新しい受入基準とも分けます。
追加の数値判定は`python scripts/check_stable_recovery.py /private/native.json`で再計算します。
全10秒窓50件以上、計測区間と重なる自然holdの累計1%以下を別々に判定し、事前宣言した
注入hold1件だけを除外します。結果は補助判定で、時計/ACK・資源・精度・元環境復元の証明は
独立したguardian検証が必要です。入力rawや既存のcontinuous/strict判定は変更しません。
Piでの10分・1時間の認識継続と自動復帰、そのhold時間・復元結果は
[自動復帰の有限検証記録](rust-recovery-validation.md)を参照してください。

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

固定/適応予約での10分・1時間指定試験の結果は
[rust-duration-validation.md](rust-duration-validation.md)を参照してください。

### Bounded dual-clock diagnostic (#603)

`cargo build --locked --features clock-probe --bin frame-clock` adds a Linux-only
observer. The CLI is `frame-clock ADDRESS CAMERA`; it requires explicit
`ESP32CAM_RUST_CLOCK_PROBE=120`, a positive `ESP32CAM_RUST_TRIAL_EXPECTED_BOOT`,
and the selected camera credential in `ESP32CAM_RUST_CLOCK_PROBE_KEY`. Pass the
credential privately through the finite supervisor, never in argv or logs.

After the authenticated hello it observes for 120 seconds, with one outstanding
random-nonce ping, a 50ms target interval and a 1s RTT/response limit. Connection
and hello waits are bounded by 20s and 5s. The last pending exchange may finish
within its 1s limit. The supervisor must also enforce a 150s process deadline,
1MiB stdout/64KiB stderr limits, resource monitoring, and original-environment
restoration on every exit. At least 1000 complete exchanges are required.

Each of at most 4096 records pairs the same Pong with MONOTONIC and MONOTONIC_RAW
readings. Device time is in microseconds; host brackets are in nanoseconds and
the report names every field. Send observations precede the write; receive
observations follow the read. Use the lower pre-send and upper post-receive
values for conservative offset intervals, not the brackets as wire timestamps.
Failure reports retain completed exchanges. Numeric stdout is capped at 1MiB.

The observer sends no profile command, performs no inference and saves no images.
All live/performance acceptance flags remain false. Neither the existing clock,
200ppm assumption, 500ms protection nor recovery policy changes. RAW removes NTP
frequency corrections, including calibration; it is not automatically more
accurate. Compare both against the same device exchanges before selecting a
future candidate. See [Linux clock semantics](https://man7.org/linux/man-pages/man3/clock_gettime.3.html)
and [rustix clock_gettime](https://docs.rs/rustix/1.1.5/rustix/time/fn.clock_gettime.html).

### Opt-in RAW host clock for finite continuous trials (#605)

The finite supervisor may set `ESP32CAM_RUST_TRIAL_RAW_CLOCK=1` together with
`ESP32CAM_RUST_TRIAL_AUTO_RECOVERY=1`. Missing/`0` retains `Instant` and adds no
RAW clock reads. Invalid values, a non-continuous request or an unsupported
platform fail before loading the model. The production Python clock is unchanged.

One immutable source/epoch supplies all numeric ping timestamps, frame age,
arrival/worker elapsed time, mode freshness and trial measurements. RAW read or
range errors stop processing; there is no fallback or mid-trial clock switch.
Independent socket wait timers retain `Instant`; their durations are not mixed
with freshness timestamps. UTC supervisor health tickets remain separate too.

Reports state `host_clock_source`, optional `host_clock_epoch_ns`, and a
`host_clock_witness` containing named MONOTONIC/RAW brackets before/after the
RAW trial. These bracket the process lifecycle, including model warmup and Eco
transitions, rather than only the tracking measurement. Default witnesses/epoch are null. Keep this evidence with the raw
report and verify the selected source before applying the ordinary full gates.

The 200ppm assumption, 500ms protection, 1s RTT, 10s evidence expiry, generation
ACK and 10s/50-result recovery are unchanged. A 120s paired-clock probe was
consistent with 200ppm for both sources; it did not reproduce the earlier
inconsistency or establish NTP as its cause. RAW is a bounded follow-up candidate,
not a proven improvement or a default rollout. Stable 5Hz still requires the
predeclared 600s → full restoration → 3600s acceptance.

Linux の有限 continuous 試験だけで `ESP32CAM_RUST_TRIAL_QUICK_ACK=1` を指定すると、
ソケット接続時と positive read のたびに `TCP_QUICKACK` を再設定する。通常は無効。
Linux以外、continuous以外、不正値、ソケット設定失敗は拒否する。レポートの
`tcp_quick_ack` に設定を記録する。TCP ACK は画像の採用・推論完了・モーター許可を
意味せず、既存の500ms、時計、generation/ACK、復帰条件は変えない。
部分画像の受信停止に対する実験候補であり、原因や実機での改善は未確認。
[Linux tcp(7)](https://man7.org/linux/man-pages/man7/tcp.7.html) にあるとおり
QUICKACK は一時的な設定なので、接続時の設定だけでは継続しない。

Linux の有限 continuous 試験で `ESP32CAM_RUST_TRIAL_SMALL_MSS=1` を指定すると、
新しいlistenerの `TCP_MAXSEG=512` をbind/listen前に設定し、SYN-ACKで小さいMSSを通知する。
既定のlistenerとQUICKACKは変更せず、両opt-inの同時指定、不正値、非Linux/非continuous、
設定失敗は拒否する。reportの `tcp_maxseg_requested` / `tcp_maxseg_accepted` に設定値と
接続後のローカルsocket値を残す（通常はnull）。後者は相手の実packet長を証明するものではない。
画像byte・推論・保護条件は維持するが、packet数とCPU負荷は増え得るため実機で比較する。
受信停止の原因や改善は未証明で、既定有効化や継続運用の合格を意味しない。

Linux有限continuous試験だけで `ESP32CAM_RUST_TRIAL_ISOLATE_IO=1` を指定すると、
monitor/ORT/Workerの生成前に現在threadをCPU1〜3へ制限し、Worker生成後・listen前に
main I/O threadだけCPU0へ移す。生成済みの子threadは1〜3を保持する。3推論threadと
モデル・保護条件は維持し、既定ではaffinityを読み書きしない。CPU0〜3が許可されていない、
設定失敗、readback不一致、非Linux/非continuousは拒否。reportは `cpu_affinity_policy` を記録する。
OS全体のaffinityやgovernorは変更しない。I/O threadから後で生成されるthreadはCPU0を継承し得るため、
実機guardianでmain TID=PIDは[0]、それ以外の全current/new TIDは[1,2,3]を上限付きで継続確認する。
CPU0のIRQ競合や性能効果は未確定で、有限試験の候補として扱う。
# 有限試験の動的タスク分割

Linuxの有限 `frame-live` continuous試験でのみ、
`ESP32CAM_RUST_TRIAL_DYNAMIC_BLOCKS=1` によりORTの `session.dynamic_block_base=4` を選択できます。
最大120入力のオフライン `frame-worker` 比較には `ESP32CAM_RUST_BENCH_DYNAMIC_BLOCKS=1` を使います。
未指定/`0` は従来の設定で、このconfig entryを追加しません。不正値はモデル読込前に拒否します。
出力の `dynamic_block_base` は従来ならnull、選択時は4です。
これは選択した設定値の記録で、ORT内部状態の独立したreadbackではありません。

[ORT公式のばらつき対策](https://onnxruntime.ai/docs/performance/tune-performance/troubleshooting.html#i-am-seeing-high-latency-variance)
を比較するための設定です。FP32、モデル/runtime、3推論thread、spinning無効、前後処理、
500ms鮮度・時計・ACK・復帰条件は維持します。既定の `Detector::new()` は設定を変更しません。
Piで固定入力の生出力・検出結果をgoldenと比較してから実機試験へ進みます。
反復するオフラインA/Bで、平均・throughput・温度を悪化させず処理時間の裾が改善することも
OTA前に確認します。比較順序、試行回数、採用基準は計測前の固定planに記録します。
設定の追加だけでは認識精度や5Hz安定性の成功を意味しません。

# 推論Run中だけのintra-op spinning

ORTの `session.force_spinning_stop` は、最後の同時 `Run()` が戻った時点で内部thread poolのspinを即停止し、
次の `Run()` で再開する設定です（SessionOptionsのconfig key、per-session内部poolにのみ適用、値は文字列 `"1"` のみ有効）。
Linuxの有限 `frame-live` continuous試験でのみ `ESP32CAM_RUST_TRIAL_SPIN_ON_RUN=1` で選択できます。
`0` を明示すると従来設定のまま、後述のオンライン中止判定だけが有効になります。
最大120入力のオフライン `frame-worker` 比較には `ESP32CAM_RUST_BENCH_SPIN_ON_RUN=0|1` を使います。
未指定/`0` はintra-op spinning無効で、config keyを追加しません。不正値（`""`、`2`、`true` 等）はモデル読込前に拒否します。
選択時はintra-op spinningを有効にし、inter-op spinningは無効のまま、Static分割・FP32・入力320・3推論threadを維持します。
global thread pool・external poolは使いません（`force_spinning_stop` が効かなくなるため）。
出力の `spin_on_run`・`intra_op_spinning`・`force_spinning_stop` は選択した設定値の記録で、ORT内部状態のreadbackではありません。

オフライン比較では `ESP32CAM_RUST_BENCH_FRAME_GAP_MS=0|40`（未指定は0）で、2入力目以降の処理の直前に40msの待機を入れます。
N入力でN-1回、最後の入力の後には入りません。これは推論Run間の遊休をCPU時間に反映させるためのもので、
`wall_us` と `usage.cpu_ticks` は各入力の処理完了後に同時点で採取します。

## オンライン中止判定

連続hold・自然hold予算・閉じた10秒窓の認識数・保護値を `src/online_abort.rs` がnativeの単調時計で評価します。
評価は受信ループの各周回（読取りタイムアウトを含む）で行い、結果の公開有無に依存しません。
保護値は次の4つです。鮮度以外は評価時点の時刻で経過を更新します。

- 入力の鮮度（500ms）は、**結果を公開する瞬間にだけ**評価します（v2仕様 D1b）。公開される結果の年齢上限が500,000µsを
  超えれば（ちょうど500,000は許容）、公開時点の検査（下の `State::publish_guard`）がそれを拒否し、A14で中止します。
  公開済みの各結果も `age_high_us > 500,000` を検査します（hold の内外とも）。
  **公開の無い tick では、hold が宣言済みかどうかに関わらず鮮度を評価しません**（直近に受理した入力を現在時刻まで更新して
  比べる「tick鮮度項」と、その適用範囲を決める `publishing` は廃止しました）。公開が止まっている間の保護は、復帰条件
  （新generationのACK・直近の公開から500ms以内・cadence 等）と、連続15秒（A11）・自然hold合計の予算（A12）・10秒窓50件
  （A13）です。閾値500msは変えていません。公開が止まる15秒 hold では、A11 より先に A13（約10秒、窓が空になる）が出ます。
  A11 が単独で出るのは、hold 中も公開が続く場合だけです。
  **既知のリスク K2（解消済み）**: 以前は、nativeのholdが「最後の公開から500ms超」で宣言されるのに対し、tick鮮度項が途絶の
  最後の公開から約127〜311msで500msを超え、hold 宣言より前に A14 が出ました。tick鮮度項の廃止で、この経路はなくなりました
  （合成時系列の単体テストが、宣言前・宣言後・hold 中のtickで A14 が出ないことを固定しています）。残る事実: 保存済みライブ6本の
  94途絶区間は hold で扱われ、次の公開結果の `age_high` は全て500ms以下（後続のある84区間で最大346ms）ですが、hold の合計時間が
  長く（途絶時間の合計 11.5〜37.7秒/本、1回の最大約10.2秒）、6本とも A12（予算6秒/600秒）を大きく超えます。live で A11/A12 が
  出る可能性が高く、出たら不合格で、基準は緩めません（CEOへ報告）。
  なお、公開時点の検査・公開済みサンプルの検査は結果を公開するモードで分岐しないため、Eco 中の公開にも同じ検査が適用されます
  （D1b は広げも狭めもしていません）。
  常に（hold の間も）評価するもの: 公開済みの各結果の `age_high_us > 500,000`、公開時点の検査（違反なら公開拒否）、
  clock の1秒（未応答）・10秒（同期）・200ppm、評価不能（計測中）。
- 未応答のclock pingの経過が1秒を超える。
- 最後に受理したclock応答からの経過が10秒を超える。
- 時計誤差の推定が200ppmを超える。保持している同一bootのclock交換の組ごとに、オフセット区間の中点の傾き（ppm）を求め、
  200ppmを超えれば違反です（ちょうど200ppmは許容）。中点の誤差は区間幅（往復時間）の半分以下なので、組の傾きの不確かさは
  両幅の和の半分を機器時間差で割った値以下になります。この不確かさが200ppmを超える短い組は推定が定義できないとして評価しません
  （往復時間の幅を時計誤差と取り違えないため）。tracking中のpingは50ms間隔で、通常保持の直近8件は約0.4秒分しかなく、往復時間が
  数msでは200ppmを分解できません。そこで直近8件とは別に、同一bootの**基準交換**を保持します。機器時間30秒ごとの期間
  （`device_us / 30s`）の中で往復時間の幅が最小の交換を1件ずつ、直近4期間分（最大約2分）です。基準は時計誤差の推定にだけ使い、
  年齢の上下限には使いません。期間が4つより古い基準は捨て、推定は常に直近の時計を表します。bootが変わると基準も捨て、新しい
  bootで取り直します（古いbootの傾きを新しいbootに持ち込みません）。サンプルの順序・巻き戻りの検査は従来どおりです。
  あわせて、2つのオフセット区間が200ppmの幅を足しても重ならない場合も違反にします（区間の整合性検査。基準と通常保持の全ての組が対象）。
- 上の推定が**評価不能**（どの組も短くて分解できない）の間は、違反なしとは読みません。計測区間の開始前は開始ゲートが待ち、
  計測区間の中では保護違反として中止します（計画§2-9(iv)・§13-9の「内部値を評価に使えなければ開始禁止／判定不能」の扱いを、
  計測中は中止の側に倒した解釈）。開始ゲートは、初期Ecoの間に推定が定義されるまでTrackingを要求せず、60秒待っても定義されなければ
  `clock_unresolved`で終了します（計測は始まっていません。reportの `reason` は `clock_unresolved`、`measurement` は null、
  終了コードは通常の非0（3）で、20（中止）ではありません）。定義できる最短の基線は、往復時間5msの2交換（幅の合計10ms）で25秒です。
  再起動復旧（`restart_expired`）はclock証拠を捨てるため、計測中なら評価不能として中止になります（その前に1秒超の未応答で
  RTT違反が先に立ちます）。

結果を公開する時点でも、同じ保護値を公開時刻で検査します（`State::publish_guard`）。違反中は公開せず、
拒否した事実をcontrollerが次の周回で受け取って中止します。保護値は計測区間の開始前から、hold・窓は計測区間内で評価します。
違反を検出するとreportに `reason`（`continuous_hold`・`natural_hold_budget_exceeded`・
`window_below_50`・`protection_violation`）を書き、終了コード20で終了します。閾値は元の値のままです。

`clock_unresolved` は中止ではなく「開始禁止・測定なし」です。live checker（`scripts/spin_on_run/live_checker.py`）は
`reason == "clock_unresolved"` かつ `measurement` が null で、`online_abort` の report（中止の記録）が無い report をこの区分に対応づけ、A07 にも F01〜F07 にも数えません
（合格でも不合格でもなく、マイルストーン未達の判定不能。試行は消費し、`max_attempts` は変えません）。guardian の復旧は
`session_final.version` を使用済みgenerationとして扱います。最終結果は `not_started`（復元が失敗していれば
`not_started_restore_failed`）です。preflight のFW同一性・S実装不可などの O07 は従来どおりです。

## R3: 全フレーム記録（`ESP32CAM_RUST_TRIAL_FRAME_LOG`）

v2 spec D7。公開間隔が伸びた区間の原因（到着側・推論側・その他）を後から分けるための**計測専用**の記録です。
制御・公開・判定・タイミングに影響する分岐は持ちません（記録の値を読み返す経路は無く、`frame-live` の決定は記録の有無で変わりません）。

- **有効化**: 環境変数 `ESP32CAM_RUST_TRIAL_FRAME_LOG` に出力ファイルの絶対パスを指定します。未設定なら無効で、従来と同じコードパスです
  （記録用の分岐は `Option` の判定1つ）。値は厳格に検証し、空・相対パス・`.`/`..`/`//` を含む・末尾`/`・制御文字・4096バイト超・
  親ディレクトリが無い・パスが既に存在する（シンボリックリンクを含む）・Unicodeでない値は起動拒否です。
  有限試験（`ESP32CAM_RUST_TRIAL_AUTO_RECOVERY=1` の continuous 試験）以外で指定した場合も起動拒否です。
- **記録**: 受信側は `Stats.frame_log`、worker側は `State` の中に、起動時に容量分を事前確保した固定容量のバッファを持ちます
  （容量は `(試験秒数+120)×12` 件。3600秒試験で44,640件、受信176B+worker104B で約12.5MB）。実行中のI/Oはありません。
  worker側の追記は既存の `State` ロックの保持中に、すでに決まった値をコピーするだけです（時計の読み取り・確保・I/Oなし）。
  容量を超えた記録は捨てて数え、header の `lost_records`（受信側とworker側の合計）に出します。
- **書出し**: 計測と報告（stdout の native.json）が終わった後、`frame_log::write_once` が1回だけ `frames.jsonl` を
  `O_EXCL`・モード0400で新規作成します（`fsync`なし）。書出しの失敗は無視され、ファイルが無いこと自体が欠測です。
  終了コードと報告は変わりません。
- **`frames.jsonl`**: 1行目が header、以降が受信した順の1フレーム1行です。時刻はすべて native 単調時計のマイクロ秒です
  （`captured_us`/`sent_us` だけカメラ時計）。

```
{"v":1,"clock":"native_monotonic_us","nominal_interval_us":200000,"capacity":N,"records":n,"lost_records":m}
{"sequence":..,"boot_id":..,"generation":..,"first_byte_us":..|null,"last_byte_us":..|null,"bytes":..,
 "received_us":..,"captured_us":..,"sent_us":..,"begun_us":..|null,"finished_us":..|null,"published_us":..|null,
 "compute_us":..|null,"stage":"..","reads":..,"max_read_us":..,"read_timeouts":..,"tcp":null|{...}}
```

| field | 意味 |
|---|---|
| `first_byte_us` / `last_byte_us` | そのフレームを完成させた socket read のうち、最初／最後にバイトを返した read の直後の時刻。先の read のバッファに既に入っていたフレーム（連続到着）は自分の read が無く null（`reads`=0）。直前のメッセージの最後の read が次のメッセージの先頭を含む場合、その分は前のメッセージ側に数えられる |
| `bytes` | WebSocket メッセージ全体の長さ（envelope+JPEG） |
| `received_us` | 既存の到着時刻（`Receiver::receive` が read 直後に取る値） |
| `begun_us` / `finished_us` / `compute_us` | workerが推論を始めた／終えた時刻と所要時間。推論に入らなかったフレームは null（`*_before` の破棄、受信側の破棄、未処理） |
| `published_us` | 公開された時刻。公開されなければ null |
| `stage` | `published`、worker側の破棄（`cancelled_before`/`clock_before`/`mode_before`/`age_before`/`cancelled_after`/`clock_after`/`mode_after`/`publication_gate`/`inference_failed`）、受信側の破棄（`order`/`mode`/`age_limit`/`clock_unavailable`）、`busy`（提出が Busy）、`overwritten`（1枠のキューで後続フレームに置換された。後続にworker記録があるとき）、`unprocessed`（終了時にキュー内か処理中） |
| `reads` / `max_read_us` / `read_timeouts` | データを返した read の回数／最初の read を除く read 呼び出しの最大所要時間／最初のバイト以降の50msタイムアウト回数（最初のバイトより前の待ちは数えない） |
| `tcp` | `TCP_INFO`（`total_retrans`,`retrans`,`lost`,`rto_us`,`rcv_rtt_us`,`at_us`）。**現在は常に null** |

**`TCP_INFO` を取れない理由**: 既存の依存（`rustix` 1.1.5 は sockopt API に `TCP_INFO` が無い、`socket2` 0.6.3 も無い）では取得できません。
生の `getsockopt` は `libc` の直接依存（`Cargo.lock` が変わる）と `unsafe`（クレートは `#![forbid(unsafe_code)]`）が必要なので、
新規依存を足さない方針に従い縮退して `tcp` を null にしています。再送の証拠は R2（`ss -tin`）で補います。`tcp_snapshot`（`transport.rs`）が
この取得点で、将来依存を足すならここだけを差し替えます。

### オーバーヘッドと影響しうる点

設計上の上限は 1 フレームあたり「受信 read ごとの観測（時計1回＋`Instant`1回）＋記録1件の固定長コピー（受信側1件＋worker側1件）」で、
目標は 50µs/フレーム以下（5Hz で 0.025% 未満）です。`getsockopt` は未実装なので現在は含まれません（実装した場合は syscall 1回分を加算）。

測定（Mac, Apple M5 Pro, arm64, `cargo test --release`、`frame_log::tests::cost_of_recording_one_frame`（`#[ignore]`）、200回の最良値、3回実行して同じ値）:

| 項目 | 値 |
|---|---|
| 1フレームの read 観測（30回のデータ read＋1回のタイムアウト。時計は `Instant` の差） | 約 0.50µs |
| 受信側の記録1件＋stage更新（176B） | 約 0.005µs |
| worker側の記録1件（104B） | 約 0.003µs |
| 終了時、18,000フレーム分の整形（JSONL 5.4MB） | 約 5.2ms |
| 終了時、同じ内容の書出し（`O_EXCL`、tmp） | 約 0.6ms |

上の値は温かいキャッシュでの最良値で、実際の `Instant::elapsed` と host clock 呼び出し（1 read あたり数十ns〜）は含みません。
31回の read でも桁として 1〜2µs/フレーム（目標の 50µs の 1/25 以下）と見積もります。**ARM の Pi では測っていません**（Mac のみ）。

タイミングに影響しうる点（計画の既知のリスク §16 に転記する）:
1. **受信ループの read ごとの観測**: read 1回につき時計とInstantの呼び出しが増えます。受信 thread の時間を使い、`max_receive_call_us` の分布がわずかに右へ動く可能性があります。
2. **記録の書込み**: 事前確保した領域に順に書くため、初めてのページに触れるときのページフォルト（約4KBごと＝受信側で約23件ごと）が実行中に起きえます。キャッシュ・メモリ帯域への影響もあります（Pi では未測定）。
3. **worker側の追記**: `State` ロックの保持時間が数ns〜数十ns延びます（公開判定のロックと同じ区間）。
4. **終了時の整形と書出し**: 約12.5MBまでを整形・書出しする間、プロセスの終了が遅れます（Mac で約6ms、Pi の SD カードでは未測定）。報告の stdout は先に書かれるため、報告内容には影響しません。supervisor が子プロセスの終了を待つ時間が伸びるだけです。
5. **未測定**: Pi 上の実測値、`getsockopt`（未実装）、記録 ON/OFF でのスケジューラ・CPU・熱の差。記録 ON で公開間隔が変わるかは、実機の A/B でしか確認できません。

テスト: `session::tests::recording_on_and_off_make_identical_decisions`（同じ合成時系列・注入時計で ON/OFF の公開集合と順序・破棄 stage・hold・A14 の判定・終了コードが一致。
容量2の欠測でも一致）、`frame_log::tests`（環境変数、容量超過、schema、`O_EXCL`、read統計、書出し関数が終了時の1か所だけであること）。

## 実験用ハーネス

`scripts/spin_on_run/` に標準ライブラリのみのPython実装があります（offline 16 runのABBAランナー、
候補Mのスレッド別CPU sampler、判定表、live用guardian・復旧guard・checker、systemd unitの生成）。
これらは生成とドライランができるファイルで、Piへの配布・実行は別の工程です。`python -m scripts.spin_on_run.cli units`
でunit文字列と起動要求のドライランを新規ディレクトリに出力できます。

### 開始の関門

`offline-run`（S・M）と `live ... execute` は、開始前に `start_gate` を通ります。`preflight`（元unit・Eco・ACK・hold・ready、
FW読取り経路、固定ファイルのSHA、空き容量、温度、liveでは復旧guardの設置・enable・`--check`・Linger・常設guardとの競合）と
`manifest.verify`（凍結したPythonファイルとbinaryのSHA、登録した計画のSHA、field対応表）のどれかが不合格なら何も起動せず、
理由を `preflight.json`（offline）または `start-refused.json`（live）に残します。offlineは終了マーカーと `result.json`（V1）も書き、
終了コードは2です。後続unit（`offline-post run`／`final-check`）も凍結ファイルの変化をO08として扱います。復旧guard・`enqueue` は関門を通しません
（開始条件が崩れていても復元は実行するため）。configには `binaries`（`frame-worker`・`frame-live`）、`manifest`、`recover_unit_path`、
`old_guard_units`、候補Mでは `pr616_worker` が必要です。実行するbinaryは検証したbinaryと同じパスです。

候補Mは登録済みの変更なしworker（SHA `91a7e709…`）を使います。このworkerにはフレーム間隔の設定が無いため、
ハーネスが結果行を受け取ってから40ms待って次の入力を渡します（`pace_gap_s`）。samplerのCPU使用率はsampler自身のプロセス
（`/proc/self/stat`）で測ります。liveの毎秒監視は元のPython serviceのAPIを使わず、ローカルの読取りだけで行います。

liveでは、開始の関門の通過後、試験unitの主プロセス（guardian）が元の `esp32cam-server.service` を `systemctl --user stop` で
ちょうど1回止めてからframe-liveを起動し、その後に毎秒の監視を始めます（再試行や再停止はしません）。停止後の状態確認で
`inactive` にならなければframe-liveを起動せず、終了マーカー `abort A04` と `service-stop.json` を残して終了します。
元のserviceの復旧は、従来どおり独立した復旧guardだけが行います。

### 公開間隔の基準（v2仕様 D5）

`scripts/spin_on_run/interval_gate.py` が、完走（`reason == "completed"`）したliveの600秒・3600秒runの `native.json` から公開間隔を
計算し、run の証跡ディレクトリに `interval-gate.json` を書きます（復旧guardの試験後検査 `post_run` が1回だけ実行）。
offline の S・M は公開間隔を持たないので対象外です。完走以外（中止・`clock_unresolved` など）には適用しません。

- **通常区間（N2）**: 計測区間 `[start_us, end_us)` 内の一意な公開結果（キー `(boot_id, sequence)`）を `published_us` 昇順に並べ、
  隣り合う2件を1間隔 D とします。除外するのは**世代をまたぐ D > 500ms の間隔だけ**（hold開始の途絶。件数を `excluded` に記録）。
  同一世代の D > 500ms は除外せず、世代をまたぐ D ≤ 500ms も含めます。重複・sequence非増加・boot_id/generation不一致は従来どおりF06です。
- **ゲート（C。合否に含め、既存の全条件にANDで追加）**: p99 ≤ 250ms（250,000µs）。パーセンタイルは線形補間
  （`k = (n−1)·p/100`）を整数・有理数（`fractions.Fraction`）で計算し、浮動小数の丸めで境界が動きません
  （p99 がちょうど250,000は pass、250,001相当は fail）。最小件数は 4.5×秒（600秒で2,700件、3600秒で16,200件）で、未満は
  `gate: "unjudgeable"` です。
- **判定と優先順位**: 不合格は新コード **F08**（`interval_p99`）、判定不能は新しい最終結果 **`interval_unjudgeable`**
  （不合格でも合格でもなく、マイルストーン未達。試行は消費し `max_attempts` は変えません。復元が失敗していれば
  `interval_unjudgeable_restore_failed`）。優先順位は 中止（A）＞ 開始禁止・測定なし（`not_started`）＞ 不合格（F01〜F08）＞
  判定不能 ＞ 合格です。既存のF/A/Oコード・判定・閾値は変えていません（`max_gap` 500msは報告専用のまま）。
- **目標（A。報告のみで、ゲートではない）**: p99 ≤ 250ms かつ最大 ≤ 300ms（300msを超える間隔が0件）なら `target: "met"`、
  そうでなければ `not_met`。間隔数が最小件数（4.5 × 秒）に満たないとき（`gate` が `unjudgeable`）は、p99 と最大が条件を
  満たしていても `met` にせず `not_judgeable` とします。導出（鮮度の導出: 500 − 伸びた間隔の age 項 B の観測最大 180.5 = 319.5ms 未満 → 切り捨てて300ms）を
  `derivation` に添えます。
- **run ごとに報告するもの**: `n`、`excluded`、`p99_us`（と厳密値 `p99_us_exact`）、`max_us`、`over_us`（>250/>300/>350 の件数）、
  `gate`（pass/fail/unjudgeable）、`target`（met/not_met/not_judgeable）、`queue_busy`（`receiver.queue_busy`）、`receiver_dropped`
  （`receiver.dropped`）、`worker_dropped`（`worker.dropped`）。閾値と最小件数は登録後に動かしません。
- 保存済みの6 runで、提案の N2 の値（`criteria-scoring.json` の `per_run_N2`: n・p99・最大・件数）を再現する回帰テストが
  あります（`tests/test_spin_on_run_interval_gate.py`）。測定区間を持たない3本（affinity・capture・mss）は、提案と同じく全公開結果
  から起動・終了端の4.5秒以上の途絶だけを除いて数えます（liveのゲートは測定区間のある完走runにだけ適用されるので、この経路は
  再現専用です）。

### R2: 試験中のPi読み取り専用サンプラ（v2仕様 D6、計測のみ）

`scripts/spin_on_run/r2_sampler.py`（Mac側）が、読み取り専用のshell `r2_remote.sh` を**標準入力で渡して**
`ssh <host> sh -s -- <camera_ip> <seconds> <process> <interface>` を1本だけ実行し（Pi上にファイルを作りません）、標準出力を
Mac側の `r2-sampler.jsonl`（新規作成のみ）に保存します。起動は `python -m scripts.spin_on_run.cli r2 <host> <camera_ip> <out>
--seconds <秒>`（`--ssh`、`--ssh-option`（例: `-i <鍵>`）、`--process`、`--interface`、`--check`）です。鍵・認証情報は引数/設定で渡し、
出力には書きません（ssh の標準エラーも捨てます）。

- **読む項目**（Piに無いものは `null`、続行）: `ss -tin dst <camera ip>`（retrans・lost・rto・rcv_rtt・lastrcv）と
  `/proc/stat`・`/proc/pressure/cpu`・`scaling_cur_freq`・frame-live の `/proc/<pid>/task/*/schedstat`（実行遅延）を約10Hz、
  `/proc/net/snmp`・`/proc/net/netstat`・`/proc/net/wireless`・`iw dev <if> station dump`・`vcgencmd get_throttled` を1Hz。
  時刻は Pi の `/proc/uptime`（`pi_uptime_s`）と Mac の単調時計（`mono_ns`）の両方です。
- **読み取り専用の担保**: リモートscriptが使うコマンドは `read`・`echo`・`ss`・`iw`・`vcgencmd`・`pgrep`・`sleep`・`getconf` と
  シェル組み込みだけです。`tests/test_spin_on_run_r2_sampler.py` が静的許可リストでscriptを検査し、書込み・削除・kill・systemctl・
  sudo・tee・ファイルへのリダイレクト（`/dev/null` を除く）・cp/mv などを含まないことを固定しています。テストは fake ssh（ローカルのsh）
  で、scriptの文面そのままを偽の `/proc` に対して実行します。
- **Mac側ツールであり、Piでは実行しない**: `r2_sampler.py` は Mac から Pi へ ssh する唯一のモジュールで、Pi 上では実行しません
  （`cli r2` は Mac で呼びます）。ただし現状、Pi へ凍結・配置するパッケージからは**除外していません**。`cli.py` が `r2` サブコマンドの
  ために `r2_sampler` を import するため、パッケージから外すと Pi 側の `cli` が起動しません。また `manifest.FROZEN_FILES` は
  `r2_sampler.py` と `r2_remote.sh` のSHAも固定します（読み取り専用の担保を含む内容を、試験中に差し替えられないようにするため）。
  `tests/test_spin_on_run_cli.py` がこの現状（FROZEN_FILES への登録、`cli.py` と `manifest.py` 以外が参照しないこと）を固定し、
  ssh 禁止テストは `r2_sampler.py` だけを名前で除外しています。除外するには、`cli.py` での遅延 import と FROZEN_FILES の見直し
  （Mac側用のmanifestを別に持つ）が要り、凍結の範囲が変わるため、このPRでは行いません。
- **欠測の扱い**: ssh の起動失敗・非0終了・途中切断・`@end` 無しの終了は、`gap` レコードとして記録するだけで、呼び出し側の
  例外にも試験の合否・進行にも影響しません（再接続はしません）。
- **オーバーヘッドの予算**: サンプラ全体（リモートのsh・ss・pgrep・sleep などの子プロセス込み）のCPU使用は1コア（4コア中）の
  10%以下（全体の2.5%以下）です。サンプラ自身が `/proc/self/stat`（shell自身と回収済みの子の utime・stime・cutime・cstime）を
  1Hzで出力し、`r2 ... --check`（と終了時の `end` レコード）が予算超過を判定します。超過はR2データへの注意として報告するだけで、
  合否には影響させません。sshd 側のCPUは含みません。**Piでは未測定**で、これを既知のリスクとします。

### 伸びた間隔の分類（v2仕様 D8、report専用）

`python -m scripts.spin_on_run.interval_classify <evidence_dir>` が、R3の `frames.jsonl`（と任意で R2 の `r2-sampler.jsonl`、
時計の対応づけに `native.json`）から、D > 250ms の公開間隔を **到着側（arrival）／推論側（inference）／その他（other）** に分類し、
`interval-classification.json` を書きます（F/A/合格には使いません）。規則（`cause-isolation-plan` §4.1 の案を固定。閾値は登録時確定）:

- **到着側**: 間隔（published_k〜published_{k+1}）内に受信を完了したフレームのうち、first_byte→last_byte が公称の撮影間隔200msを
  超える、または隣り合うフレームの first_byte の間隔が撮影間隔+100ms（300ms）を超える。
- **推論側**: 間隔内に推論を終えたフレームの `compute_us` ≥ 200ms、または間隔内に推論を始めたフレームの received→begun の待ち ≥ 100ms。
- **その他**: どちらにも当たらない。主分類の優先順位は 到着側 → 推論側 → その他。複数に当たれば両方のフラグを残し、主分類は先に
  当たったものです。**カメラ起因**（`captured_us` の間隔 > 250ms）と **CPU競合**（R2の schedstat 実行遅延の合計 ≥ 50ms）は
  排他にしない別フラグです。R2 が無い、または時計を対応づけられない（`native.json` の `host_clock_witness` が無い）場合、
  CPU競合は `null`（未判定）で、R2 なしでも分類は動きます。
- **出力**: run ごとの件数、分類別件数、フラグの件数、各間隔の `d_us`・フラグ・主分類・理由・（あれば）TCP と R2 の補助値
  （再送・OFO・lastrcv・実行遅延・周波数・throttle）。`frames.jsonl` が無い run は `classifiable: false`
  （`reason: "frames_jsonl_missing"`）と報告し、`native.json` があれば D > 250ms の件数だけを添えます。

### R2・R3 の出力の置き場所

liveの証跡ディレクトリ（`native.json` と同じ場所）に置きます。

| ファイル | 内容 | 生成元 |
|---|---|---|
| `frames.jsonl` | R3の全フレーム記録。Pi側のstage dirに書かれ、試験後の回収でMacへ | frame-live（`ESP32CAM_RUST_TRIAL_FRAME_LOG`） |
| `r2-sampler.jsonl` | R2の読み取り専用サンプラの出力 | Mac側 `r2_sampler.py` |
| `interval-gate.json` | D5の公開間隔の報告 | 復旧guardの `post_run`（`live_checker`） |
| `interval-classification.json` | D8の分類（report専用） | `interval_classify.py` |

v2のlive起動では、launch envに `ESP32CAM_RUST_TRIAL_FRAME_LOG=<stage dir>/frames.jsonl`（絶対パス）を含めます
（`units.live_env(..., run_dir=...)`、`units` サブコマンドのドライランも同じ）。凍結manifestは `live_env_keys` と
`frame_log`（env名とファイル名）を持ち、開始の関門が、凍結内容の変化、launch envの値が `<stage dir>/frames.jsonl` と一致すること、
stage dir に `frames.jsonl` がまだ無いことを検査します（不一致は `frame_log_env`・`launch_env_unreadable`・`frame_log_exists` として
O05）。guardian の毎秒の容量検査では `frames.jsonl` に専用の上限（32MiB。3600秒でも約10MiB）を使います。offline（S・M）の起動には
付けません。
