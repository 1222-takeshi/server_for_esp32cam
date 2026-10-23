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
