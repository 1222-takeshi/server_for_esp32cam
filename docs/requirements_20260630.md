# Raspi Migration and AI Detection Snapshot Option Requirements

## 目的
- Mac 上の ESP32-CAM サーバーを Raspberry Pi に移行し、LAN と Tailscale からアクセスできる運用にする。
- Raspi のストレージ制約に合わせて snapshot/video の rotation を設定する。
- AI detection 成功時だけ snapshot を保存対象にできるオプションを追加する。

## 要件
- Raspi 上で Docker Compose または systemd によりサーバーを再起動可能にする。
- サーバーは `0.0.0.0:8080` で待ち受け、LAN 内端末から `/healthz/ready` と Web UI にアクセスできる。
- Tailscale 経由の URL/IP と確認手順を runbook に記録する。
- `ESP32CAM_DEVICE_API_KEY(S)` と `ESP32CAM_ADMIN_TOKEN` を本番運用で必須にする。
- `ESP32CAM_STORAGE_RETENTION_MAX_BYTES` と `ESP32CAM_VIDEO_RETENTION_MAX_BYTES` で容量上限を設定できる。
- `ESP32CAM_KEEP_ONLY_DETECTED_SNAPSHOTS=true` のとき、推論成功かつ detection が 0 件の snapshot は保存しない。
- 推論スキップまたは失敗時は、データ損失を避けるため snapshot を保持する。

## テスト観点
- 正常: detection ありの snapshot は保持され、`/api/detections` に記録される。
- 正常: detection なしの snapshot は、オプション有効時にファイルとメタデータから削除される。
- 異常: detector 例外時は snapshot を保持し、`inference_status=failed` を記録する。
- 境界: retention の byte 上限が最新 upload を保護しつつ古い snapshot を削除する。
- セキュリティ: token 未設定警告、upload 認証、rate limit、JPEG 検証を維持する。
