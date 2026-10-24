# Rust frame worker PoC

Issue #553のオフライン互換・性能検証用です。ECF2をstdinから有限回再生し、認識結果と計測値を
JSON Linesでstdoutへ返します。実行中は1画像・待ちqueueなし、最大120画像・出力1行64KiBです。
不正/途中EOF/重複/逆順/モデル故障/出力故障で非0終了し、無限retryしません。

本番Pythonサービスへは接続しません。WebSocket認証、時計同期、profile ACK、実時間の500ms鮮度、
eco/tracking制御、保存/通知を提供しない段階です。全結果に`replay_only=true`、
`freshness_checked=false`、`actuation_state=hold`を強制します。実機の新鮮な認識結果や
motor commandへ流用してはいけません。オフラインHzをcamera live Hz/10Hz達成とは扱いません。

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
CLIおよび本番Pythonサービスへの接続は別段階で、モータ制御・live Hz達成を意味しません。

任意feature `transport`の`Connection`はcamera path/API keyを検証するLAN内WebSocketです。
TLSは提供せず、本番listenerへの接続はまだ行いません。handshakeは接続開始から2秒/8KiB、
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
