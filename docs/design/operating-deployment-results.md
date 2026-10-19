# 運転モードの実装・Pi反映と同時負荷の記録

2026-10-19 JST、[#505](https://github.com/1222-takeshi/server_for_esp32cam/issues/505) /
[#506](https://github.com/1222-takeshi/server_for_esp32cam/issues/506) の手動モード・容量対策を実装し、
既存Raspberry Pi 4とESP32-CAMへ反映した。Macは操作端末だけで、受信・JPEG前処理・推論・保存はPiで行う。
この記録は実装・実機反映と、未完了の受入を分ける。本人猫の精度・実W数・10Hz認識・motor駆動の達成とは扱わない。

## 実装と固定条件

- native backend [#525](https://github.com/1222-takeshi/server_for_esp32cam/pull/525)、
  管理UI/API [#527](https://github.com/1222-takeshi/server_for_esp32cam/pull/527)。
  認識は実行中1枚＋最新待機1枚、旧boot/世代の画像・結果を新世代へ付け直さない。
- YOLO11n FP32 ONNX、320px、threshold0.5、ORT3threads、QVGA/JPEG12。
  SHA256 `ff1013da8734fe2b8ede5f34a2124855d447fbefd248d148e9f29e7f8e28c9c4` を起動時に照合。
  両モードとも同じモデル・前後処理・画質を使用し、量子化や入力縮小による高速化はしない。
  Pi隔離試験のcommit `0111155` と反映code `badfe24` の `script/onnx_detector.py` /
  `script/detector.py` は同一blob。ただし撮影間隔による短い出現の見逃しや現場precision/recallは別評価。
- 単一の共通firmwareでeco 5秒 / tracking 190msを切替。管理者認証・世代CAS・device ACK・
  切替後の新しい認識結果と実測周期を確認する。起動/切断はeco、追跡準備はobserve_only、他はhold。
- 最新画像/状態はRAM、preview最大2Hz。画像はカメラごとに1枚/10秒以下（writerは最大4カメラ）、保存queueは実行中込み2件。
  keep-only時は無検出を保存前に除外する。代表画像だけを推論完了状態で原子的にSQLiteへ登録する。
- 常用ログはサービス合計10MiB×3、認識集計は60秒ごと。詳細診断は既定OFF、明示的に最大600秒/合計32MiB。
  Web/nativeの出力を同じcollectorにまとめ、stdout/journalとの二重永続化を避ける。
- 画像8GiB/30日、device logs全体10,000rows/7日、DB256MiB/WAL8MiB、worker実write_bytesと
  論理予約128MiB/h、低容量hysteresisを監視する。これは物理hard quotaやSD寿命保証ではない。
  保存停止中も保持制御を定期実行し、保持故障中の復帰を拒否する。正常60秒＋手動復帰でecoへ戻す。

## 実機反映

検証済みrelease全体 `52cd6309b1311e66489e0b70563818b8df79f55e` を別ディレクトリへ展開し、
systemdの参照先を切り替えた。元のsource/uploads/models/data/`.env` とraw/GT/baselineは保持。
DBはonline backupを作成し、新旧コードの読出し互換をコピーで確認（snapshot1,572件を維持）。
保持制御の対象は運用データであり、端末ログの期限切れ削除前のDB backupもprivate領域に残した。

反映処理には実行時間上限と排他を設け、中断時・次boot時の復旧unitを用意した。
旧unitの復元だけで復旧完了とはせず、reload/restart成功、新PID/start_ticksと元cwd、
旧firmwareの新しいdesired-config ACK、基準値後の画像3回更新とreadyを確認する。
元firmware/configは保持し、反映失敗時の復旧手順はmock故障注入7件と2名のread-only reviewで検証した。
本番で意図的な電源断やdisk-fullを発生させた結果ではない。

同じcamera bootのまま次を確認した（短時間の切替確認、30分受入とは別）：

| モード | 実測認識Hz | motor状態 | 反映 |
|---|---:|---|---|
| eco | 0.198829 | hold | ACK＋複数周期 active |
| tracking | 5.224741 | observe_only | ACK＋新frame＋cadence active |
| eco | 0.197334 | hold | 新世代ACK＋複数周期 active |

[#530](https://github.com/1222-takeshi/server_for_esp32cam/pull/530) のDashboard監視と
[#532](https://github.com/1222-takeshi/server_for_esp32cam/pull/532) の修正を含む
`31dca868cadb5beca5702be5586179ceac8c0870` へ、共通firmwareのままcodeだけを再反映した。
初回更新はWeb readyの後のnative API起動待ち不足（503）で失敗し、元release52cd630へ復旧した。
復旧でも起動待ちを補強し、元cwd・新PID/start_ticks・同一boot・eco activeまで確認してから再試行した。
失敗run `update-20261019T051656Z` とhelper原本も保持する。修正版は故障回帰を含む24件と2名のreviewを通過。

再試行 `update-20261019T052029Z` は成功。同じbootのままeco 0.200111Hz → tracking 5.3Hz →
eco 0.200733Hz、ACK/新世代/active、hold/observe_only/holdを確認した。
code専用更新は旧unitへ戻し、DBやfirmwareを過去へ戻さない。sourceを切替前・commit前にもhash照合する。

[#534](https://github.com/1222-takeshi/server_for_esp32cam/pull/534) を含む
`c1bda0b351135e1696752d6cdd7abcfcb014eac8` へのcode-only更新
`update-20261019T122338Z` もcommitまで成功した。同じcamera bootでeco 0.200372Hz →
tracking 5.3Hz → eco 0.199939HzのACK/activeとhold/observe_only/hold、新PID/start_ticksと
新releaseのcwdを確認した。モデル・firmware・元データは同じである。

[#536](https://github.com/1222-takeshi/server_for_esp32cam/pull/536) の同期区間保持を含む
release `badfe24` の最初の更新run `update-20261019T123805Z` は、初期eco要求のHTTPエラーで失敗した。
status codeは旧helperに記録されず、起動直後の世代競合は仮説に留める。
復旧は旧release c1bda0b・新PID/start_ticks・同一camera boot・eco active/holdまで確認した。
helperはcameraの接続と元bootを待ち、最新状態の取得とCAS要求を同じ制限時間内でやり直すよう補強する。
この失敗run/旧helperも保持し、次回からHTTP statusを秘密値なしで記録する。
保護中はecoに限って正常60秒/resume_readyを最大90秒待ち、CAS付きresumeを行う。
trackingからの自動resumeは禁止し、待ち切れなければ未復旧として残す。helperの回帰は31件と2名のreviewで検証する。

再試行 `update-20261019T124314Z` はcommitまで成功した。同じcamera bootのまま
新release `badfe24beba46086f6fca20742e4fdb45f7f47d7`、新PID/start_ticks/cwdを確認。
eco 0.199861Hz → tracking 5.3Hz → eco 0.200160HzのACK/activeとhold/observe_only/holdを確認した。

管理画面の描画、RAM画像更新、管理者token入力欄、要求/実効/保護理由の表示をブラウザで確認した。
使用方法とAPIは[運用手順](../operating-service.md)。管理者tokenは既存設定を使いブラウザに永続保存しない。

同じrelease badfe24のecoを64.833秒、同じnative PID/start_ticks/世代で観測した。
新frame/新結果13件（0.200516Hz）、Linux `/proc/PID/stat` のCPU ticks差分からnative CPU平均10.766%。
これはプロセスの短時間CPU測定で、Pi全体の電力や通信bytesの削減率ではない。

## 同時負荷・長期観測

最初の同時負荷run `acceptance-20261019T000306Z` は約293秒、
`acceptance-20261019T045219Z` は約161秒で保護へ移り、不合格として保持した。
後者は `frames_expired` で、受信時年齢上限486.619msの遅着画像に154.238msの推論を使い、
その後の新しい画像の結果間隔が約625msになった。保護停止そのものは正常に働いた。

[#532](https://github.com/1222-takeshi/server_for_esp32cam/pull/532) は実測861件の推論P95
161.56msを基に165msの余裕がない画像を推論前に除外する。最大220.986ms等の遅い処理には
期限切れが残り得るため、完了時500ms検査も維持する。同じPRで、10秒区間の5Hzを満たす
290ms jitterでも旧3秒平均が誤保護した問題を修正した。最初の結果を含む連続10秒区間ごとに
50件以上を確認し、正確5Hzの境界と到着前watchdogも回帰テストした。基準を低Hzへ緩めていない。
旧native workerで抑止されていた60秒集計INFOも専用loggerで有効化し、他moduleのINFOは増やさない。

`acceptance-20261019T052229Z` も116.198秒で不合格。今回は推論自体が498.605msへ延び、
後続422.640msの処理も期限切れとなった。820件の推論median/P95は148.371/169.211ms。
また、試験側の模擬writerの資源監視が切替・warmup待ち中にstaleとなり、保存1件だけだったため
十分な保存/通知負荷をかけた結果とも扱わない。次のhelperは開始前から独立1Hz監視を続け、
異常を直ちに不合格として記録する。SSH接続に伴うOSの `landscape-sysinfo` のCPU使用も観測したため、
測定を乱さないよう1本の持続接続で観測する。推論遅延との因果関係を確定した主張ではなく、OS設定や他serviceは変更しない。

`acceptance-20261019T053309Z` は持続SSH接続と独立資源監視でも31.059秒で不合格となった。
独立資源監視はstaleにならず31秒で模擬保存/通知8件・監視異常0件を確認したが、`frames_expired` は再発した。
直前の新しい結果の推論は155.536msで、完了後500ms以内に次の結果が届かなかった。
別の箇所でもhost受信間隔479.944ms・受信時年齢上限457msを観測した。記録の最大gap490.767msは
完了した結果同士だけの値であり、次の結果が来ないまま保護へ移る末尾の空白を含まない。
診断の停止後の挙動は十分記録されず、camera送信・無線・host schedulingの原因は確定していない。

Piが `wlan0` を使用し、省電力がONであることをread-onlyで確認した（signal −39dBm）。
[#534](https://github.com/1222-takeshi/server_for_esp32cam/pull/534) は追跡要求中だけ既存のclock pingを
50ms目標へ変更し、次のping期限に受信待ちを短縮する。ecoは2秒のまま、未応答nonceは1個、
1秒timeoutと500ms保護を維持する。OS設定は変更せず、通信途切れへの効果は実測で確認する。

`acceptance-20261019T122643Z`（v5、code c1bda0b）はtrackingのactive確定前に
`frames_expired` となり、30分測定を開始できなかった。模擬保存/通知は3件で、負荷受入とは扱わない。
受信間隔491.849msの遅着画像は処理予約で除外され、次の画像も最新clock exchangeの
不確かさにより年齢上限437.670msとして除外された。直前までの有効な8exchangeを個別の
200ppm driftと元の10秒期限で交差すると、次の画像の年齢は40.455–50.484msとなる。
[#536](https://github.com/1222-takeshi/server_for_esp32cam/pull/536) で同期情報の保持を修正した。
ただし、この余分な除外を防ぐだけで500ms以内の連続認識や通信途切れ解消を保証しない。
保護中は要求eco/holdを維持。v5のcleanupは資源guard中のmode要求を拒否され失敗したため、
別途正常60秒を待って手動resumeし、eco active 0.200230Hz/hold/ACKを確認した。
次のhelperは不合格を保持したまま、最大100秒の正常待ちとCAS付きeco復帰も検証する。

最新run `acceptance-20261019T124556Z`（v6、code badfe24）もwarmup中の
`frames_expired` で不合格となり、30分測定は開始できなかった。追跡世代の推論76件は
min/median/max 146.218/151.6835/169.969msで、今回の末尾では推論自体の長時間化は見られない。
同じ2画像のcamera capture差179.863ms・send開始差190.161msに対して、Pi受信差496.942msだった。
次の新画像は受信時の年齢上限54.786msで採用できたが、その推論完了は前回の公開結果から
515.621ms後となった。既に500ms watchdogで保護され、結果は `profile_changed` として公開せず破棄した。時計区間保持だけでは通信途切れを吸収できなかった。
模擬保存/通知4件・監視異常0件、cleanup異常なし。正常60秒後のCAS resumeと
同一bootのeco active 0.200063Hz/ACK/holdまで確認した。不合格は不合格のまま保存する。

Pi側Wi-Fi省電力OFFでの比較は未実施。現 `ubuntu@raspi-server.local` 接続はsudoにパスワードが
必要で、NetworkManagerもないため、この比較には管理権限を使える接続または利用者側の設定操作が必要。
省電力・無線再送・cameraの送信待ち・host schedulingの根本原因を確定したものではない。
同じ条件で失敗する試験を合格扱いせず、次の比較を[#506](https://github.com/1222-takeshi/server_for_esp32cam/issues/506)へ残す。

試験は実カメラの認識に加え、RAM preview・health・
カメラHTML/保存一覧を閲覧し、隔離した運用データへ同じbounded writer/SQLite保存と
localhostへのWebhookを最大1枚/10秒で実行する。模擬検出を本番の認識結果や現場精度へ混ぜない。
この統合試験は native の完了済み10秒区間を確認する。以前の#503の全体再受入ではなく、
測定開始に位相を合わせた全10秒窓、wire欠落率、RAW時計整合の再測定は含まない。
遅延P95 250ms、最大結果間隔500ms、drop1%、推論失敗0、CPU300%、RSS増加32MiB、
温度80℃未満/throttle0、health遅延・preview欠落0・eco復帰を明示的に判定する。

24時間read-only auditは2026-10-19 00:04:49 UTCに開始した。1分周期・証拠最大8MiBで、
DB/WAL、row数、画像index bytes、ログ占有、空き容量、温度、CPU/RSS、mode/Hz、
service各PIDのLinux `write_bytes`差分とroot filesystem deviceの全ホスト書込みを分けて記録する。
PID退出の間に発生した書込みは取りこぼし得るため、service値は退出がある場合は下限となる。
実際の手動モードを観測し、24時間連続tracking・全画像猫ありのworst caseとは扱わない。
途中のコード更新・プロセス退出も保持し、単一releaseで24時間連続稼働した結果へ読み替えない。
05:25:50 UTCに開始したv2 auditでは各行に実source commit/cwd・PID/start_ticksを前後で確認し、
観測中の更新を識別する。取得失敗時のidentityは不明として残し、前回値で埋めない。
24時間経過前に結果を推計で置き換えず、#505/#506の未受入を残す。

2026-10-19の最終試験直後の常用ログ占有は1,158,537 bytesで30MiB枠内、native集計445件・最大1,912 bytesだった。
これは途中時点の占有であり、1日の物理書込み量や全期間の上限検証ではない。
小さな[結果manifest](operating-deployment-results.json)にsource/固定条件/証拠SHA256を追跡し、
raw・helper原本・失敗run・DB backupはPi/Macのprivate領域に保持する。大型生成物はGitへ追加しない。

2026-10-20 JSTの追跡確認（10/19 22:47 UTC）では、同じsource/PIDのecoが `cadence_low` で
資源保護中だった。約0.20Hzの認識自体は継続し、正常60秒/resume_readyを確認して手動resumeした。
記録 `checkpoint-20261019T224707Z` で新世代ACK、eco active 0.199964Hz、holdまで確認した。
無停止・常時正常だったとは扱わず、保護状態も監査の原本に残す。

同時点のaudit途中値は次の通り。いずれも24時間のresultファイルはまだない。

| audit | 経過 | 取得成功/失敗 | active / degraded / pending の観測行 |
|---|---:|---:|---:|
| 初回 00:04:49開始 | 81,720秒（約22.7h） | 1,362 / 1 | 829 / 530 / 3 |
| v2 05:25:50開始 | 62,460秒（約17.3h） | 1,042 / 0 | 658 / 382 / 2 |

これは1分間隔の観測行数で、実時間uptimeではない。HTTP readyでも認識状態がdegradedの行があり、
取得成功を運用合格へ読み替えない。v2は3releaseを含み、service write_bytes差分13,631,488 bytesも
退出17プロセスの取りこぼしを含み得る下限である。全host書込み1,571,262,464 bytesとは分ける。
保護中に任意書込みを停止している期間を含むため、これらから通常負荷の24h書込み合格を推計しない。
常用ログ占有は途中で2,364,638 bytes、別枠の手動release/試験/backup領域は前回duで293,093,376 bytes。
常用ログ30MiB枠は、この手動領域やモデル・raw/GT/baselineの全容量を制限するものではない。

## 次の受入

本人猫/猫なし・昼/消灯の独立GT、短い出現の見逃し、実W数、10Hz認識、物理校正と
モータなし角度simulationは未完了。公開fixtureのthread互換性は以前の記録を維持し、現場precision/recallへ読み替えない。
自動昇格/deep sleep/実motor駆動は別の受入で、手動モード切替によって駆動を許可しない。
