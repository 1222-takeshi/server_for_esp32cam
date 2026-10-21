# Raspberry Pi上の運転モード用native service

このbackendは管理UI/APIの接続後に有効化します。既存productionのソースが古い場合は、一部の
ファイルだけを上書きせず、検証したrelease全体をbackup/rollback付きで切り替えてください。
uploads、models、data、`.env`、保護済みraw/GT/baselineを保持します。

共通firmware `esp32cam_operating_profiles` とnative workerが1cameraを担当します。モデルは
YOLO11n FP32、入力320、threshold0.5、ORT3threads。採用済みSHA256
`ff1013da8734fe2b8ede5f34a2124855d447fbefd248d148e9f29e7f8e28c9c4`以外は起動時に拒否します。
依存関係は`.[server,onnx,capture]`。既存のweb processとは別にnative processで動かし、
Gunicornのgevent request内でONNX/Pillow/SQL保存を実行しません。

必要な環境設定例（秘密値は既存のcamera-bound設定を使用）:

```dotenv
ESP32CAM_OPERATING_CAMERA_ID=esp32cam-01
ESP32CAM_OPERATING_MODEL_PATH=/app/models/yolo11n.onnx
ESP32CAM_OPERATING_SOCKET=/run/user/1000/esp32cam-operating/control.sock
ESP32CAM_OPERATING_DIAGNOSTIC_DIR=/home/ubuntu/.local/state/esp32cam/diagnostics
ESP32CAM_OPERATING_PORT=8766
ESP32CAM_DEVICE_LOG_RETENTION_MAX_ROWS=10000
ESP32CAM_DEVICE_LOG_RETENTION_MAX_AGE_SECONDS=604800
```

camera IDは最大64文字。native専用依存ディレクトリがある場合は
`ESP32CAM_OPERATING_PYTHONPATH`を追加できます。socket親ディレクトリは専用の700、socketは600。
marker/flockで管理された停止済みsocketだけ再作成し、他のdata/通常file/symlinkは削除しません。

serviceのExecStartは既存collectorの子をsupervisorへ変更します。Gunicorn/nativeworkerは同じ
process group・stdout/stderrを継承するため、サービス合計30MiBのログ枠を共用します。

```sh
python -m script.log_runner --directory /home/ubuntu/.local/state/esp32cam/logs -- \
  python -m script.operating_service supervise -- \
  gunicorn -k gevent -w 1 --timeout 120 --graceful-timeout 30 -b 0.0.0.0:8080 script.server:app
```

どちらかの子が終了するとsupervisorが終了し、collectorが残る子も停止します。systemdの
`Restart=on-failure`でecoから再起動します。単独native起動は`python -m script.operating_service worker`。
WebSocketキー/管理者トークンはコマンド引数・ログへ渡しません。

認識は実行中1枚＋最新待ち1枚。最新JPEG/状態はRAMで、previewのためにSDへ書きません。
保存はcamera全体10秒/枚以下、実行中込み2件。keep-only時は無検出を保存前に除外します。
保存threadは推論済みsnapshotとdetectionsをSQLiteへ一括登録し、通常推論へ再投入しません。
sidecarにboot/generation/sequence/model SHAを記録します。captured_atはclock同期からのUTC推定で、
sidecarの`capture_time_estimated=true`で示します。切替後の古い待ち保存は拒否しますが、
既に実行中の保存は旧世代のprovenanceを付けて完了することがあります。

画像quotaは最大8GiB/30日（既存のより厳しい設定を優先）。device logsは最大10k/7日。
nativeの独立maintenance threadが起動時と1時間ごとに保持制御を実行するため、無検出や保存停止中も
期限切れの運用画像を掃除します。負の保持設定は起動時に拒否します。
保持処理の故障時は30秒ごとに再試行し、成功するまで保存・trackingへの切替・resumeを拒否します。
成功後も正常な資源監視60秒と手動resumeが必要です。
DB/WAL/空き/論理予約量/worker実write_bytesを既存guardで監視し、80℃以上・throttle非0・監視故障も
任意書込み抑止と保護eco/holdへ接続します。正常60秒＋手動resumeでecoへ再開し、trackingへ自動昇格しません。
これは物理hard quotaではなく、実行中書込みや他processの超過を含む実bytesの測定が必要です。
raw削除・自動VACUUMはしません。診断は既定off、明示的に最大600秒/32MiBで開始します。

private controlのrequestはJSON1KiB、JSON reply64KiB/JPEG128KiB、4handler、読込全体500ms。
`camera_id,op`を必須とし、opは`status/frame/mode/resume/diagnostic.start/diagnostic.stop`。
modeは`mode,expected_version`、resumeは`expected_version`、diagnostic.startは`duration_seconds`を付けます。
管理者認証は外部Flask APIで行い、UNIX socketをHTTP/TCPへ直接公開しないでください。
認識Hz・age histogram（上方向1ms丸め）・drop/保存/通知/診断・CPU/RSS/温度/資源をRAMで参照し、
常用ログは60秒集計です。実機30分同時負荷、24h書込み、現場精度、実W数の受入は別に記録します。

管理画面は `/cameras/<camera_id>` の「撮影・認識モード」から操作できます。
`ESP32CAM_OPERATING_SOCKET` と `ESP32CAM_OPERATING_CAMERA_ID` が一致するカメラだけ有効です。
`ESP32CAM_ADMIN_TOKEN` を設定し、画面のpassword欄へ入力してください。ブラウザの永続storageには保存しません。
GET `/api/cameras/<id>/operating` は要求/実効/ACK/資源、GET `.../frame` はRAM JPEGです（鮮度超過204、不通503）。
PUT `.../operating` は `mode,expected_version`、POST `.../resume` は `expected_version` をJSONで渡します。
診断はPOST `.../diagnostic` に `duration_seconds`、停止はDELETEに空のJSON objectを渡します。
変更APIはBearer管理者認証必須・body最大1KiB。未対応camera404・世代競合409・未設定token/worker不通503です。

trackingのactive判定は最初の新しい認識結果を含む `[開始, 開始+10秒)` の観測を待ち、
連続する完了済み10秒区間ごとに50件以上の新規結果があることを確認します。
観測完了後のHzも直前の完了区間の計数です。短い3秒区間の揺れだけで
保護へ移ることを避け、採用済みの10秒基準に合わせています。50件未満なら保護eco/holdです。
結果間隔500ms超過の即時保護、世代照合、モデル/画質/thresholdは変更しません。
Pi native workerでは推論前に165msの処理余裕を予約し、現在の画像年齢上限との合計が500msを
超える画像は推論せず、次の新しい画像へ進みます。実測861件の推論P95 161.56msを切り上げた
ヒューリスティックであり、実測最大220.986msを含む遅い推論では期限切れが残り得ます。
完了時の500ms検査も維持し、実機負荷試験で再発・Hz・最大gapを検証します。モデルの計算や出力は変えません。
旧実験receiverの既定予約は0msです。`insufficient_inference_time` をdropへ計上します。

Pi native workerのtracking要求中は、既存の認証済みclock pingを50ms目標（最大20Hz）で送ります。
JPEGが来ない間も次のping期限までに受信待ちを終え、ecoへ戻ると従来の2秒周期へ戻します。
未応答nonceは1個のみで、1秒の応答timeoutを維持します。旧receiverの既定値は2秒です。
Pi側Wi-Fi省電力ONと受信の途切れを実測したための通信頻度対策であり、OS設定は変更しません。
[Wi-Fi省電力はAPによる遅延送出を伴い得ます](https://wireless.docs.kernel.org/en/latest/en/developers/documentation/ieee80211/power-savings.html)が、
このPiの途切れの原因や対策効果を確定するものではありません。追加の小さなping/pong通信はtracking限定で、
実機のHz・遅延・CPUを再測定します。認識回数へpingや補間を加算せず、常用ログは60秒集計のままです。

時計同期は同じ接続・bootの直近最大8 exchangeから、各frame受信時に有効な年齢区間の
共通部分を使います。各exchangeの元の受信時刻から10秒で失効し、capture時刻の前後両方向の
200ppm drift余裕を個別に含めます。新しい遅延pongだけで、まだ有効な狭い区間を捨てません。
区間の矛盾・明らかな未来時刻・有効sampleなしでは推論へ渡さず、boot変更/再接続では過去を引き継ぎません。
これは既存の時計rate誤差仮定の下での境界改善で、200ppmが実機の保証値になるものではありません。
500ms freshnessと165ms処理予約は同じです。実機同時負荷の合格は別途測定します。

native workerは `session.intra_op.allow_spinning=0` / `session.inter_op.allow_spinning=0` を明示し、
処理待ち中のactive spinを抑えます。一般ONNX detectorの`allow_spinning`既定値はTrueで従来互換です。
[ORT公式説明](https://onnxruntime.ai/docs/performance/tune-performance/threading.html)の通り、
spin無効化はCPU/電力と推論待ち時間のtradeoffなので、実機でHz・温度・出力互換を確認します。
モデル・前後処理・3threads・500ms freshness・80℃保護は変更せず、W数の低下や30分合格を先取りしません。

## 24時間観測の集計

v3の `manifest.json` / `samples.jsonl` / 完了後の `result.json` を原本のまま保管し、
集計は完了したrun、または別ディレクトリに凍結した途中コピーに対して実行します。
Python標準ライブラリだけを使い、認識processやmodeを変更しません。

```sh
python scripts/summarize_operating_audit.py \
  --run /private/audit24h-RUN --output /private/audit24h-summary.json \
  --require-complete
```

入力はsamples最大8MiB、manifest/result各64KiBに制限します。既存outputや原本の上書き、
読込中の入力変更、不正な行/累積counter/完了件数は拒否します。`--require-complete` は
24h経過・1,441行・resultの取得件数整合に加え、各sampleが予定時刻から60秒以内の遅れ
（丸めの早着許容1ms）・最大gap120秒以内を要求し、揃わなければexit3でoutputを作りません。
`observer_result_completed`（collector終了）と `sampling_coverage_complete`（網羅性）は別々に記録します。
省略時は途中値を `observation_completed=false` と明示して記録できます。
不正な入力や既存outputはexit2、集計成功はexit0です。出力は同じdirectoryの一時ファイルに
write/flush/fsync/closeした後、排他的hard linkで確定し、途中失敗で不完全なJSONを残しません。

source/boot/mode/WLANの行数、保護・不明sample、DB/WAL/logの観測最大、最小空き容量を
記録します。service書込みは退出processで取りこぼし得る下限、host書込みは他serviceを含む値です。
行数比を実時間uptimeにせず、完了しても `normal_load_24h_accepted=false`、
`tracking_24h_accepted=false`、`watts_measured=false` を維持します。
実際の手動eco観測だけで、猫が常に写る通常負荷・現場精度・実W数を合格扱いにしません。

完了時の集計を予約する場合は、auditの終了予定後に `--require-complete` を実行する
一回のuser timerを使います。未完了exit3のみ有限回retryし、既存output/不正入力のexit2は
停止させます。observer自体の再起動・mode変更・raw削除や通知送信は行いません。
