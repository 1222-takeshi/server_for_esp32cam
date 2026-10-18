# 同じfirmwareでの省エネ／追跡準備切替

`esp32cam_operating_profiles` は明示的にbuildする新しいtargetです。通常の`main.cpp`、
実験用`esp32cam_high_rate_capture`、既存の整数秒desired configを維持します。
このtargetだけではserver/UIへの接続は完了せず、単独で本番flashしません。

認証済みlive WebSocket上で次を交換します。boot/generationは正整数、generation上限は
JSON/UIで正確に扱える`2**53-1`。キーは既存のcamera-bound `X-API-Key`で接続時に確認します。

```json
{"type":"profile.hello","schema":2,"boot_id":123,"generation_floor":455,"profiles":["eco","tracking"]}
{"type":"profile.set","boot_id":123,"generation":456,"mode":"tracking"}
{"type":"profile.ack","boot_id":123,"generation":456,"mode":"tracking","applied_us":789}
```

serverはACK deadlineを5秒とし、requestedの変更だけではactiveにしません。後続のcontrollerで
boot/世代一致ACKと適用後の新しいframe/cadenceを確認します。ecoは5000ms、trackingは190ms。
QVGA/JPEG12を共通に保ち、motorを動かす処理はありません。電力削減率は別測定です。

起動・WebSocket再接続はeco/世代0から始め、新commandを待ちます。切断やサーバから5秒間
連絡がない場合もtrackingを解除します。同じbootで見た最大世代は切断後も保持し、serverはhelloの
`generation_floor`より大きい新世代でecoを要求します。古い世代・同世代で異なるmodeは拒否し、接続中の同世代同modeは
適用時刻を変えず冪等ACKします。適用以前のcamera bufferを新世代として送信しません。

ECF1は既存40byteのままです。ECF2はbig-endianの48byte headerで、順序は
`magic(4), boot(8), sequence(8), captured_us(8), sent_us(8), generation(8), jpeg_length(4)`。
JPEG上限128KiB、世代0のframeは送信/受入しません。Python decoderは`allow_profiles=True`でのみ
ECF2を受け入れます。JPEGの受信時に現在の世代を付け直すことは禁止します。

`CaptureReceiver(profile=OperatingMode(), continuous=True, result_sink=...)` が対応経路です。
受信loopは100ms以下の待ち時間でcommand/watchdogを処理し、helloなし5秒で切断します。
ACK一致後のECF2だけを推論し、世代変更時は旧待ち画像を破棄、推論中の旧結果も公開前に拒否します。
撮影clockからの500ms鮮度制限と実行中1枚＋最新待ち1枚の上限は共通です。
`result_sink(frame, event)` は新世代のJPEGと検証済み結果を有界・非blockingなRAM/queue処理へ
渡すためのものです。disk/SQL/HTTP通知を直接呼ばず、各writerへ委譲してください。
continuousは対応profileがある場合だけ使用でき、既存の有期限実験receiverは既定のままです。
