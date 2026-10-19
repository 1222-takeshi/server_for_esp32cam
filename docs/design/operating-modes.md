# 容量と負荷を制限する運転モード

- Source: 2026-10-18の利用者提案「容量が少ないのでログを抑え、モータトラッキングと省エネを分ける」。設計Issue [#504](https://github.com/1222-takeshi/server_for_esp32cam/issues/504)、常用統合 [#486](https://github.com/1222-takeshi/server_for_esp32cam/issues/486)、既存ログ最適化 [#417](https://github.com/1222-takeshi/server_for_esp32cam/issues/417)。
- Status: **native backend・管理UI/API・共通firmwareと容量制御は実装し、2026-10-19にPiへ反映済み**。[実機反映と測定状況](operating-deployment-results.md)、[運用手順](../operating-service.md)を参照。以下の設計時の根拠と候補は履歴として保持し、24h書込み・現場精度・実W数の受入とは分ける。

## CAPABILITY

運用者が1台のカメラを省エネと追跡準備へ切り替え、認識品質の設定を保ったまま撮影/推論頻度を変える。画像・ログ・DB・待機画像を個別に制限し、残容量と実際に反映されたモードを確認できるようにする。Raspberry Pi単体で処理し、モード選択とmotor駆動許可を別に扱う。

## CONSTRAINTS

### 設計時の根拠（2026-10-18の履歴）

| 対象 | 現状と不足 |
|---|---|
| Pi処理能力 | [30分測定](raspi-high-rate-recognition-results.md)で5.262Hz、process CPU250%（1 core=100%）、最大76℃。通常保存/通知同時負荷・常用統合は未受入。10Hz撮影時の認識は6.908Hzでdrop34.44% |
| 通常経路 | [推論FIFO](../../script/inference.py)は上限なし。保存/SQLite更新後に推論する。HTTP成功ログはDEBUGへ変更済みだが、単に撮影周期を縮めると書込みとbacklogが増える |
| 画像保存 | 猫なし画像を推論後に削除しても、一度は保存する。猫が写り続ければ全画像が残り得る。「検出時だけ」では保存頻度上限にならない |
| 試験用ログ | [observer](../../script/live_observer.py)は100,000 events/128MiB/指定時間で停止。30分runのeventsは13,221,042 bytes（約12.6MiB）。同じ記録密度を24時間分へ換算すると約635MB/日だが、実測は30分で、現CLIは無期限運転しない |
| 既存の保持制御 | 画像/動画はage/byte retentionがある。[端末ログ](../../script/config.py)はage/全camera合計row上限を実装済みで、どちらも既定0（無効・opt-in）。DELETE後のDB/WAL物理サイズや書込み量の制限は別に扱う |
| Pi容量の確認 | 2026-10-18 JSTのread-only確認: filesystem 30,060,949,504 bytes、空き23,964,651,520 bytes（約22.3GiB）、journal全体39.2M、ready。確認した`.env`では画像8GiB/動画1GiB/猫なし削除を明示、端末ログageは明示なし（他の環境上書きを含むeffective値は未確認）。ホスト全体の数値で、サービス専用消費量や電力の実測ではない |

### 固定する方針

- 撮影Hz、認識Hz、保存Hz、ログ書込みHz、motor指令Hzを分離する。5Hz認識のために5Hzで保存・INFO記録・通知しない。
- 両モードで採用する猫モデルSHA、FP32/入力320/しきい値、QVGA/JPEG12・前後処理を共通にする。画質/モデル変更は独立した精度比較を必要とする。現在の人物SSDから猫モデルへの常用切替も現場精度の別受入。
- 省エネは撮影・推論回数を減らす方針。消費W数の削減率は未計測。低頻度撮影では短い出現や動きを捉え損ねるため、1枚のモデル精度と時間方向の見逃しを区別する。
- raw/GT/baselineは保護領域とし、自動rotation/低容量時削除の対象外。新規実験を始める前に容量を確認し、不要物の削除を暗黙に実行しない。
- 通常のmode選択は手動。過負荷保護による`eco`自動要求だけは例外とし、`tracking`への再昇格は手動にする。起動・状態不明・異常時はmotorをhold/停止し、実際のカメラ状態を未確認のまま「省エネに復帰済み」と表示しない。

## IMPLEMENTATION CONTRACT

### 利用者向けモード

| 項目 | `eco`（省エネ） | `tracking`（追跡準備） |
|---|---|---|
| 撮影/認識 | 初期候補5秒周期、約0.2Hz。新規画像だけ推論 | 初期候補190ms撮影、最低5Hz認識。10Hz達成とは表示しない |
| CPU/通信 | 撮影・推論回数を抑える。Wi-Fi sleepやdeep sleepは後続測定 | 最新画像を優先。3推論threadを初期候補とし、保存/通知込みで再測定 |
| 画像保存 | 必要なイベントの代表画像を上限内で保存 | 同じ保存規則。認識の全フレームは保存しない |
| ログ | 共通の低頻度集計・状態変化・抑制付き異常記録 | 同じ。フレームごとの成功INFO/座標永続記録は既定off |
| motor | 停止 | 現場精度・校正・機材/停止条件の受入前は`observe_only`。UIも「追跡準備（motor停止）」と表示 |

`tracking`は運転profile名。既存の`ESP32CAM_TRACKING_ENABLED`（軽量IoU関連付け）や`InferenceService.mode`（sync/async）とは別の概念にする。[猫追従契約](cat-following-capability.md)の駆動前条件を満たすことなくmotorを有効化するスイッチにしない。

### ログ・保存・資源の初期候補

以下は設計時の**候補値の履歴**。現在の実装・有効値は[反映記録](operating-deployment-results.md)を参照。

| 領域 | 制限と振る舞い |
|---|---|
| 常用ログ | サービス合計で10MiB×3 files相当を候補に、容量と世代数を制限。通常は60秒ごとに1集計（最大8KiB）、Hz/drop/遅延分布/CPU/RSS/温度/空き容量/保存数・bytesを記録。mode変更・起動停止・異常の発生/復帰は別記録。同一異常の連打は集約し、抑制件数も残す |
| 試験詳細ログ | 明示的な期限付き診断のみ。初期候補10分/32MiBで先に達した方で停止、全診断ディレクトリにも合計上限。通常の認識はログ上限到達で停止させず、診断停止と抑制数を状態へ出す。保護済みrawはrotation対象外 |
| 代表画像/動画 | 初期候補はカメラ全体で画像1枚/10秒以下。開始/終了画像にも同じ上限を適用し、track数で増えないようにする。画像/動画の既存byte・age quotaも併用し、認識と独立した有界writerへ渡す。動画の常時生成はoff |
| DB | 各フレームのbbox/成功ログは永続化せず、イベントと集計を保存。端末ログは全体10,000 rows/7日を初期候補とし、受付/保存時にも上限を守る。DB/WALの実bytes・書込みbytes/時間・checkpoint負荷を計測して物理budgetを決める。row削除をファイル縮小と同一視しない |
| メモリ/待ち行列 | 認識はcamera当たり実行中1枚＋最新待機1枚、overwrittenをdropへ計上。画像writer・通知・ログ出力にも件数とbytesの両上限を設け、飽和で制御/認識を待たせない。カメラ数の上限も固定して総量を制限 |
| ディスク余裕 | 空きがmax(1GiB,filesystemの10%)以下なら任意の新規画像/動画/詳細ログを抑止し、残容量と抑制数を表示。再開目安はmax(1.5GiB,15%)を60秒維持し、手動確認後に再開。未割当DB/WAL/一時ファイル/OS領域を別に確保する |
| 過負荷 | 常用統合後も最低5Hz/遅延/drop・温度80℃未満/throttle0・API応答の既存受入基準を再検証。持続的逸脱は`degraded`としmotorをhold、任意書込みを抑制、`eco`を要求する。低Hzへ落としながら追跡正常と表示しない |

RAM上の鮮度・異常判定はフレームごとに行う。60秒のログ集計まで停止判断を待たせない。CPU-boundな画像/推論/大きな削除・DB保守はgevent request greenletを塞がない専用処理へ出す。保存quotaは容量を制限してもSDへの累積書込み量は制限しないため、bytes/時間も受入に含める。

[Compose](../../docker-compose.yml)はservice別に`json-file`の`max-size=10m`/`max-file=3`を設定済み。[Docker仕様](https://docs.docker.com/engine/logging/drivers/json-file/)に従い、コンテナ再作成後の有効値を確認する。Piのsystemd運用では[専用collectorの起動手順](../deployment.md)を使う。[journaldの上限](https://www.freedesktop.org/software/systemd/man/latest/journald.conf.html)は共有journal/namespace単位であり、ホスト全体の設定をこのサービス専用の上限と誤認しない。file出力とstdout/journalとの二重永続化を含めて総量を検証する。DB保守も[SQLite VACUUM](https://www.sqlite.org/lang_vacuum.html)の必要空き領域・ロック・負荷を確認し、低容量時や撮影ごとに実行しない。

### 状態・インターフェース

- 管理者UI/APIはcamera単位の`requested_mode`、`expected_version`を受け付ける。deviceは対応profile/schema version、`boot_id`、反映したmode/versionと失敗理由を返す。statusは`requested_mode`と`effective_mode`、`pending/active/degraded/unknown`、最終ACK/新frame時刻、`actuation_state=observe_only/hold`、抑制数を分けて表示する。
- `eco -> pending(tracking) -> tracking/observe_only`は対応firmware・model ready・一致versionのACK・切替後の新frame/鮮度を確認して成立。`tracking -> pending(eco) -> eco`もACKと新frameで確認する。省エネ周期の安定は複数間隔を観測し、単一frameで低消費電力達成とはしない。
- 現行`ECF1`にはmode世代がないため、対応schemaの新フレーム形式で`mode_generation`（適用したconfig versionに対応）を必須化する。deviceは撮影時点の適用世代をframeへ付け、ACKにも同じ世代/bootを返す。receiver→推論→結果/制御までcamera/boot/sequence/世代を伝搬・照合し、受信時点のmodeを旧画像へ付け直さない。`ECF1`の既存headerは変更せず、capability negotiationで旧deviceと区別する。世代欠落・不一致・旧bootのframeは新modeの実績/制御へ採用しない。
- 切替時はmotor hold、世代番号更新、旧待機画像の破棄とtrack stateリセットを行う。実行中の旧世代の結果は後から来ても新modeの認識/制御へ採用しない。拒否/破棄数を観測可能にする。
- 競合version・未対応mode・不正値は拒否。同version再要求は冪等に処理し、古いACK/前boot ACKでactiveにしない。通信断/ACK timeoutはholdと`unknown/degraded`を示す。`eco`要求と実際の省エネ反映を混同せず再試行する。再起動時に最後のtracking要求だけで駆動を再開しない。
- [既存desired config](remote-control-api.md)の`capture_interval_seconds=5..86400`へ小数を混ぜない。単一の対応firmware内でprofileを切り替える拡張が必要。旧deviceは従来動作を維持し、tracking要求を拒否。毎回OTAでmodeを切り替える方式にしない。
- 現行設定pollは既定60秒なので即時切替を約束できない。管理画面にpendingを示し、受入前に通信方式・反映deadlineを固定する。motor停止はこのpoll/省エネへのACK待ちに依存させず、別の停止条件で成立させる。
- モード変更権限は既存管理者認証、device ACKは既存device認証/ID対応を使う。キー・画像・秘密設定をログへ残さない。未認証の観測APIからmode変更できるようにしない。

### 実装時の受入例

| 分類 | 検証 |
|---|---|
| normal | 同一firmwareでeco→tracking→eco、version/ACK/新frameと実測Hzが一致。model/画質固定の参照出力一致、motor停止のまま全経路を確認 |
| edge | 上限ちょうど/1byte超過、全画像に猫あり、複数track、debug期限/全診断合計quota、長時間ログrotation、低disk、DB/WAL増加、writer飽和でもAPI/鮮度判定が動作 |
| invalid | 未認証・別camera ACK、不正/未対応mode、競合version、旧boot/旧世代frameと結果・世代欠落、重複/順序逆転、future/stale frameを拒否しhold |
| regression | 旧firmware/5秒設定/OTA/通常upload/保存通知、静止猫で保存上限超過なし、error stormでもログ量有界、Pi再起動・通信断・ログ保存失敗でもdrive再開なし |
| 実機負荷 | Pi単体で保存/通知/閲覧/health同時のtracking30分、eco比較、複数回の往復切替、24時間の生成bytes/物理占有/SD書込み・温度・API/Hz・eco見逃しを測る。失敗runとM2元観測を除外しない |

## NON-GOALS

この設計PRは本番mode/保持設定の変更、既存画像・ログ・保護rawの削除、motor制御API・配線/駆動、機材購入、deep sleep、自動対象検出によるmode切替、モデル軽量化を実装しない。過負荷時の保護的`eco`要求は設計範囲だが、対象検出による自動昇格/復帰とは別にする。自動切替には対象不在の待機時間・最小滞在時間・hysteresisと、低頻度中の見逃し評価が別途必要。

## OPEN QUESTIONS

1. 候補のログ30MiB/診断32MiB・端末ログ件数/7日・保存10秒/枚が運用に十分か。DB/WAL・writer・カメラ数の総bytes上限、通知上限、反映deadlineと過負荷の継続判定時間は実装前に固定する。
2. 省エネ時の5秒より長い周期、Wi-Fi sleep/deep sleep、実W数と復帰時間・時間方向の見逃しの許容値。
3. 実motorの機材・校正・安全停止と昼夜GTは[猫追従契約](cat-following-capability.md)で未決。tracking modeの実装だけで駆動可にはしない。

## HANDOFF

有界ログ/保存/DB・待ち行列とACK付き手動profileは実装・Pi反映済み。[#505](https://github.com/1222-takeshi/server_for_esp32cam/issues/505)と[#506](https://github.com/1222-takeshi/server_for_esp32cam/issues/506)で、同時負荷・24h書込み・ecoのCPU/通信/実W数・時間方向の見逃しの残る受入を継続する。現場精度・同時負荷・実W数を評価した後に対象検出による自動切替、角度simulation、実motorの順で別受入する。各PRでmeaningful tests、関連GitHub Actions/local act、静的解析、2名以上のread-only reviewを行う。ロードマップ上の実装・実測完了はこの設計mergeとは分ける。
