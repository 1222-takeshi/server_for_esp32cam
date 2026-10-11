# 猫部屋の画像収集（未ラベル）

猫検知の学習・評価には、設置予定のカメラで撮った実画像と人手で確認した正解が必要です。現在の本番設定は人物検知かつ検知時のみ保存なので、保存APIから猫なし画像を集める方法では偏ります。独立CLIは新着の `/stream/cameras/<camera-id>` MJPEGを受信し、本番サービス・保存設定・timer・モーターを変更せずにJPEGを収集します。配信は過去の保存画像を再生しません。

画像には人や室内が写る可能性があります。Gitへ追加せず、Piや作業端末のprivate領域で保管してください。収集場所を変える際はカメラの安全な設置と電源・ネットワークを確認し、撮影について関係者の了解を得てください。

## 収集

`--url` は `localhost`、ローカルIP、Tailscale IPのHTTP(S)と、指定カメラの正確なstream pathだけを受け付けます。ユーザー名・パスワード・query token・redirectは拒否し、URLをinventoryや通常出力に残しません。HTTPSの証明書検証は無効にしません。接続には認証トークンを使いません。

```bash
mkdir -p ~/.local/share/esp32cam-cat-data
python3 -m scripts.collect_camera_dataset \
  --url http://100.108.195.27:8080/stream/cameras/esp32cam-01 \
  --camera-id esp32cam-01 \
  --output ~/.local/share/esp32cam-cat-data/session-20261012-day-01 \
  --lighting day
```

既定値は10分、保存間隔60秒、最大10枚、画像とinventoryの容量上限50 MiB、読み取りtimeout10秒です。各frameは最大8 MiB、実行時間は最大1時間、画像数は最大1,000枚です。別の時刻・照明条件は**新しい出力dir**へ収集してください。出力先が存在すると拒否します。例えば現位置の接続確認だけなら `--duration-seconds 20 --sample-interval-seconds 5 --max-images 3` とします。この画像は猫部屋の正例・負例には扱いません。

`images/`のJPEGは新規作成後read-onlyにし、`inventory.json`に受信UTC、カメラID、SHA-256、寸法、宣言したday/night/unknownを記録します。カメラの撮影時刻はこの経路では分からないため`capture_time_utc: null`です。受信UTCを撮影時刻と読み替えません。重複SHA、無効JPEG、途中切断、intervalで見送ったframeの件数はinventoryに残ります。条件内のframeでも容量上限に達すれば保存を止めます。`mean_luma`と`dark_pixel_fraction`は縮小画像から得た暗さの**参考値**で、照明不足や検知失敗の原因を断定しません。

## 人手確認と評価への変換

収集inventoryは**全件未レビュー**で、猫がいないことを意味する`boxes: []`を生成しません。猫部屋へ移動後は昼・夜、猫の位置と距離、遮蔽、空室、人、動く影を別sessionで収集し、重複や近接時刻の画像を同じ開発・評価分割へまとめます。撮影対象を人手で確認し、猫がいる画像に正規化bbox、確認済みの猫なし画像だけに空のbboxを付けてください。判定に迷う画像は未ラベルのまま残します。

既存の[評価設計](../design/pet-detection-evaluation.md)のmanifest v1は、各sampleに相対`path`、元画像の`sha256`、人手確認済み`boxes`だけを含みます。inventoryをそのまま評価CLIへ渡すことはできません。元JPEGを変更せず、注釈の修正ごとに新しいmanifestを作り、モデル・labels・画像のhashを固定してから評価します。保存成功や明るさだけを猫検知精度とみなしません。
