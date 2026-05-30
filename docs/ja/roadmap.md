# 将来の開発 / ロードマップ

このプロジェクトは現在、次の実用的な最初のマイルストーンにあります:

* 単一の ESP32-CAM
* JPEG スナップショット upload
* 中央サーバー保存
* ブラウザからの閲覧

今後の拡張は、このスナップショット優先の本線を崩さない形で進めます。

ペット監視 MVP、1週間連続稼働、Raspberry Pi Zero 候補、月額0円運用を前提にした詳細な導入判断と優先順位は [`docs/ja/business-scenarios.md`](business-scenarios.md) にまとめています。

---

## 導入シナリオと MVP KPI

### ユースケース

| # | シナリオ | 主な利用者 | 価値 |
|---|---------|----------|------|
| 1 | **自宅・事務所の防犯モニタリング** | 個人・小規模事業者 | LAN 内の低コスト常時撮影、人物検知アラート |
| 2 | **屋外・農場の定点観測** | 農業・研究用途 | 天候・生育状況の時系列記録、電池駆動での長期運用 |
| 3 | **DIY スマートホーム連携** | 技術者・自作愛好家 | Webhook で外部サービスへ通知、既存スマートホームと統合 |
| 4 | **複数カメラの集中管理** | 小規模施設管理者 | 最大 10 台の ESP32-CAM を 1 台の Raspberry Pi で管理 |
| 5 | **研究・開発プロトタイプ** | 開発者 | OpenAPI で他システムと統合、TFLite モデルの評価環境 |

### MVP で追う KPI

| KPI | 目標値 | 計測方法 |
|-----|--------|---------|
| 継続稼働日数 | 30 日以上 (Raspberry Pi) | `/healthz/ready` の継続モニタリング |
| 撮影成功率 | 95% 以上 (upload 成功 / 試行) | Prometheus `upload_total` / `upload_errors_total` |
| 検知通知の有用性 | 誤報率 < 20% | 人物検知スコアしきい値の調整で評価 |
| 復旧時間 (RTO) | 15 分以内 | バックアップからのリストア訓練で計測 |
| 運用工数 | 月 30 分以下 | ログ確認・容量管理・バックアップ取得の実績 |

### 優先しない機能（今はやらないこと）

以下は将来の Issue として管理しており、MVP 判断には含めません:

| 機能 | 対応 Issue | 理由 |
|------|----------|------|
| ライブビデオストリーミング | [#9](https://github.com/1222-takeshi/server_for_esp32cam/issues/9) | 電力・帯域コストが高く、スナップショットで代替可能 |
| 将来の機能拡張案 | [#20](https://github.com/1222-takeshi/server_for_esp32cam/issues/20) | 優先順位未確定 |
| 動画アップロード API | [#35](https://github.com/1222-takeshi/server_for_esp32cam/issues/35) | ストレージ設計・形式選定が必要 |

### 運用コスト・制約の比較

| 環境 | ハード費用 | 電力 | スキル要件 | 制約 |
|------|-----------|------|----------|------|
| Mac ローカル (開発) | 既存 PC | 通常 | Python 環境 | 常時稼働には不向き |
| Raspberry Pi 4 (推奨) | 約 ¥8,000〜 | 約 3〜5W | SSH・Docker | 10 台規模まで |
| クラウド (将来) | 月額従量 | — | クラウド知識 | Redis・外部ボリューム必要 |

---

## 機能ロードマップ

### 1. 単一カメラスナップショット経路の安定化 ✅

*   upload 検証と保存挙動を強化する
*   ブラウザ UI を単純かつ確実に保つ
*   deep sleep 前提の低電力スナップショット運用を改善する

### 2. 保存先 backend の柔軟化

*   ESP32-CAM からの upload は常に中央サーバー API に送る
*   サーバー側で次の保存先を切り替えられるようにする
    * ローカルディスク
    * NAS
    * Google Drive

### 3. camera-aware な扱いの拡張 ✅

*   upload 契約には最初から `device_id` と `captured_at` を含める
*   保存先 backend 拡張時に、資産をカメラ単位に整理できるようにする
*   UI 上で画像ソースを見分けやすくする

### 4. ESP32-CAM 遠隔制御APIの設計

*   deep sleep 前提のため、サーバーpushではなく device polling で desired config を取得する
*   まずは撮影間隔、解像度、JPEG品質、向き補正、再起動要求、OTA待受要求に限定する
*   OTA待受は非空OTAパスワード必須とし、次回起床時の明示的な待受ウィンドウ内だけ受け付ける
*   参照: [#137 ESP32-CAM遠隔制御APIを設計する](https://github.com/1222-takeshi/server_for_esp32cam/issues/137), [`docs/design/remote-control-api.md`](../design/remote-control-api.md)

### 5. 短い動画クリップの評価

*   連続動画は最初のデフォルトにしない
*   スナップショットだけでは足りない場合に短いクリップ upload を検討する
*   低電力運用を引き続き設計制約とする
*   参照: [#35 動画アップロード API と保存方式の設計](https://github.com/1222-takeshi/server_for_esp32cam/issues/35)

### 6. 高度機能は後回しにする

*   ML 処理
*   アラート / 通知
*   モーターなど可動部を伴うリモート I/O 制御
*   より高度な自動化パイプライン

これらは、スナップショットと保存経路が安定した後に追加します。

---

## 関連ドキュメント

| ドキュメント | 内容 |
|------------|------|
| [`docs/ja/requirements.md`](requirements.md) | 機能・非機能要件と受け入れ基準 |
| [`docs/ja/business-scenarios.md`](business-scenarios.md) | 事業・導入シナリオとKPI |
| [`docs/design/remote-control-api.md`](../design/remote-control-api.md) | ESP32-CAM 遠隔制御API設計 |
| [`docs/ja/security.md`](security.md) | 脅威モデルと運用チェックリスト |
| [`docs/ja/operations-runbook.md`](operations-runbook.md) | 運用 Runbook |
| [`docs/deployment.md`](../deployment.md) | Docker デプロイガイド |
