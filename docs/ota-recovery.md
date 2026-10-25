# OTA復旧の送信と診断（#578）

`script.ota_transport.upload()`は、hashを確認したstock uploaderを専用childから呼ぶためのadapterです。
2MiB以内のimageをSHA-256確認したimmutable RAM snapshotにし、stockのMD5/転送は同じbytesを読みます。
親によるchildの期限・cgroup回収・元FW復旧が必須で、request greenletから直接呼びません。

listenerはport0で先にbindして保持し、その実portをstockのinvitationへ渡します。
portを選んで一旦解放する競合窓は作りません。認証・転送protocol自体はstockを使います。
socketは最大16、image streamは最大4、診断は最後の8eventだけです。phase/errno以外の
エラー本文・stdout/stderr/log・画像・キーは保持しません。diskへの出力は呼び出し側の責任です。

## 検証

Pi内TCPで固定port32320の再bindがEADDRINUSEになることを再現しました。
hash固定の実stock uploaderとloopback dummy ESP32では、host/Piとも3回連続の認証・転送が成功し、
旧portの再使用はexit1、認証拒否は秘密本文なしの失敗metadataになることを確認しました。
過去の失敗本文は残っていないため、過去の全失敗原因がこれだったとは断定しません。

元profile FWだけの実機2段階OTA smoke `ota-recovery-20261025T141006Z-606b57f1` は、
予約port44335/44633、転送22.176/16.946秒、両exit0・runner成功・復旧再試行0でした。
元FW/元Python source613d293/unit不変、eco active/ACK/hold/health readyへ復帰しています。
新guardはSHA固定helperと親60秒期限を使い、診断を最大8attempt/16KiBでbest-effort保存します。
モデル・画質・500ms保護・物理motorは変更していません。旧guardとrawは変更せず保全しました。

公開23tests、private guard33mock tests、Pi合計56tests、ARM64ローカルactと全体pytestで検証。
実stock loopbackシナリオのCI常設は保守性の改善項目です。stock source/実行script/実機metadataは
private領域にSHA付きで保全し、秘密設定と大きいFWはGitに含めません。
