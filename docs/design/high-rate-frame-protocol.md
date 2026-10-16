# 高頻度frame protocol v1

[#490](https://github.com/1222-takeshi/server_for_esp32cam/issues/490)。[高頻度認識計画](high-rate-recognition.md)の隔離経路用で、既存のJPEG-only uploadへ直接送らない。

WebSocket binary messageは40-byte header（全整数network big-endian）と最大128KiBのJPEGを一つずつ送る。Python形式は`>4sQQQQI`。認証とJPEGの実体・寸法検証は受信側の責務。

| Offset | 内容 |
|---|---|
| 0 | magic: ASCII `ECF1`（4 bytes） |
| 4 | boot ID: uint64、起動ごとに生成、0不可 |
| 12 | frame sequence: uint64、最初は1、同boot内で単調増加 |
| 20 | captured_us: uint64、driver framebuffer撮影timestamp、起動後microseconds、0不可 |
| 28 | sent_us: uint64、送信直前の同じmonotonic clock、captured_us以上 |
| 36 | JPEG bytes長: uint32、message長−40と一致、0不可 |

送信失敗でも取得した新規frameのsequenceを消費し、再送や同一撮影timestampを新規認識へ計上しない。受信側はsequenceと撮影時刻の逆転・重複を拒否し、gap数を記録する。`FrameOrder`は1cameraの直近32 retired bootを保持する補助で、受信器はさらに接続ごとのbootを固定する。起動前/接続前の欠落数は推定せず不明として記録する。

時計交換はserverからtext JSON `{"type":"clock.ping","id":"<hex nonce, 1〜32文字>"}`、deviceから`{"type":"clock.pong","id":"<同nonce>","boot_id":整数,"device_us":整数}`。受信器はpending nonce・boot・timeoutを照合し、古いpongを再利用しない。host送信/受信を`t0/t2`、device採時を`t1`とすると、host−deviceのoffsetは`[t0−t1, t2−t1]`内とする。時刻源は双方monotonicで、UTC受信時刻を撮影時刻へ置換しない。

`ClockSample`は往復1秒以内の有効交換から、frameのcapture→receive遅延を区間で返す。既定はsample寿命10秒、host/device clock-rate比の誤差200ppmの仮定を、撮影時刻と交換時のdevice時刻の差へ加算する。撮影が交換より前の場合も含む。これは機器保証でなく試験前に検証・固定する仮定で、boot不一致・sample期限超過・確実に未来の撮影は不明/無効として扱う。推論完了までのserver側時間を別に加え、区間上限と時計誤差を併記する。

deviceの任意statusは`type=capture.status`、同boot ID、累積`attempts/captured/capture_failures/stale/sent/send_failures`。frame IDだけでは分からない撮影失敗も記録する。受信器はstatusとclock JSONにも型・長さ・回数の上限を設ける。
