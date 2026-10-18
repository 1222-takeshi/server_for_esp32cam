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
