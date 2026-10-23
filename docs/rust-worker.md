# Rust frame worker PoC

Issue #553 の隔離PoCです。既存Pythonサービスとは接続せず、最初はframe replayで比較します。
この段階でWebSocket認証・時計同期・profile ACK・500ms鮮度・motor制御を提供しません。
envelopeをdecodeできても、実カメラの新鮮な認識結果として公開してはいけません。

`rust/frame-worker` はECF1/ECF2のbig-endian header・正整数identity・時刻順序・世代上限・
JPEG最大128KiB・宣言長を検証します。stream readerは宣言長をpayload allocation前に検査し、
途中EOFを拒否します。JPEG実体の検査はdetectorの責務です。orderingは直近32 retired boot、
最新slotは待ち1件・非待機lockです。受信時のmodeを旧frameへ付け直しません。

```sh
cd rust/frame-worker
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
```

build成果物は既存`.gitignore`の`target/`で除外し、Cargo.lockは追跡します。
次段階で固定モデル/ORT1.23.2のdetector、続いてreplay CLIとPi比較を追加します。
既存raw/GT/baselineを変更せず、認識精度・5/10Hz・本番移行を先取りして受入しません。

## 固定profile detector

`detector`は受入済みモデルのSHAと、PiのARM64 ORT1.23.2共有ライブラリのSHAを検査します。
`ort = 2.0.0-rc.11`の動的ロードだけを使用し、ORT/modelの自動downloadは無効です。
CPU・FP32・320・3 intra threads・1 inter thread・sequential・spinning off・全graph最適化、
threshold0.5 / class-aware NMS0.7 / 全class合計max_det300を固定します。
モデルは検証した同じbytesからsessionを生成します。共有libraryは管理者専用の不変pathで、検証から初期化完了まで差し替えません。
相対pathはcanonicalな絶対pathへ解決し、その同じpathをhash検査とloaderへ渡します。

RGB入力は幅320・高さ240または320だけで、resizeせずpadding114/CHW/FP32正規化を行います。
JPEG decodeは次段階です。EXIF/他geometryはPoCの対象外です。全raw出力のfinite/shapeを確認します。
同じconfidenceの候補が複数あるとNumPy argsortのtie順序を保証できないため、結果を推測せず拒否します。
この制約も含め本番互換とはまだ扱わず、固定20画像でtensor/raw/detectionの互換を別々に検証します。

Cargo.lock追加238行は依存manifestとして不可分です。JPEG/replay/比較ツールは後続PRへ分割します。
