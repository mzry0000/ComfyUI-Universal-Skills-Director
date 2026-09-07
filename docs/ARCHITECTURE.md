# Architecture v2

更新: 2026-09-07

## Core

- nodes.py: Load Skill、Prompt Composer、旧Plannerの薄い互換wrapper。
- specification.py: browser埋込Markdown／JSONの再検証とtrusted path fallback。現行D&D UIを維持。
- composer.py: 入力snapshot、画像preview、共通Responses payload、final_promptの無改変返却。
- contracts.py: Pydanticによる唯一の型定義。送信用の限定的Schema投影とstrict object検査を行い、host側では全制約を検証。安全なフィールド位置・エラー分類を返す。
- openai_client.py: lazy SDK、workflow外API key、拒否／未完了／通信エラーの秘匿されたdomain error。
- local_config.py: 固定ush_config.json。環境変数優先。enable_directorは起動時のoperator設定。
- web/js/specification_drop.js: 1つのclick／D&D領域。元のUSH_LoadSpecification ID、標準STRINGへの埋込形式を維持。

通常経路は1回のResponses呼び出し（SDKの自動retryは別）。
モデル出力はfinal_promptとwarningsの2field。hostによる文章の再構成、句読点処理、意味的dedup、文字数truncateはしない。
Skillは制作規則、requestは今回の要求、contextは補足。モデルへ送る画像のroleはimage1等で明示する。
明示禁止とexact copyを残し、見た目の繰返しとQAは避ける。意味上の遵守や品質はschemaだけでは保証しない。

generation_idは通常のComfyUI入力としてcache invalidationに使う。API seedではない。
max_output_tokensはreasoning_effortと独立させる。store=false、toolsなし。
SDK clientはprocess内で共有し、初回API利用まで構築しない。

## Optional Director

universal_skills/director/とdirector_nodes.pyはenable_director=trueの場合だけrootからimportする。
通常利用の契約から切り離し、画像・テキストの手動進行に限定する。

    Model Draft → host Plan + Ledger
                        ↓
               Select / explicit attempt
                   ↓              ↓
            final_prompt       started_ledger + ticket
                   ↓              ↓
              既存生成ノード → Record Image / Text → Save Session
                                  ↓
                       Resolve Input → 次工程の実IMAGE / STRING

### Plan

Stageを最小単位とする。各Stageは独自prompt、media_kind、target_profile、input_bindings、parametersを持つ。
固定のScene/Shot/Variant階層や、全media共通のcamera/motion fieldは持たない。複数案は独立Stageとして表現する。
stage_idとorderは別。モデルのnew-*仮IDをhost UUIDへ変換し、依存参照も同時に付け替える。
改訂時はprevious Draftと修正requestをAPIへ渡し、既存IDを保持する。project IDはbrief hashと独立、revisionはhostが増やす。

Pydanticは形、coreはID重複、profile/media対応、存在するinput、slotの型、DAGを検証する。
Plan全体のsignatureは整合性チェックであり、署名認証や改ざん耐性を提供するMACではない。

### 再利用

definition_hashはstage prompt、media/profile、ソート済みparametersとbindings、元画像全体のfingerprint、
依存stageのdefinition_hashで作る。説明、表示order、brief、Skillそのものは含めない。
Skill変更が実promptに反映された場合に無効化され、promptが同じなら生成物を再利用できる。
execution_hashはこれに依存結果のcontent fingerprintを含める。別の画像結果へ黙って接続し直せない。

同一stage IDかつ同一定義なら完了履歴を引継ぐ。runningは改訂時にcancelledとし、明示retryを要求する。
変更・削除されたStageは新Ledgerへ持ち込まない。旧revisionのsessionを残したい場合は別session名で保存する。

### Ledger / Attempt

状態はpending（attemptなし）、running、succeeded、failed、cancelledのみ。
Selectはpendingだけを通常選択し、retryはfailed/cancelled、resumeはrunningを明示IDで選ぶ。
attempt番号とticketは開始時だけ発行。Recordはattemptを増やさない。
同じticketの同じterminal結果は冪等。異なるterminal結果による上書き、旧revisionのticket、未開始attemptは拒否する。
終わったattemptの遅延重複通知も二重計上しない。

Record Imageは生成IMAGEへのgraph edgeを必須とし、実PNGを保存してから成功を記録する。
Record Textも生成STRINGへのedgeを必須とし、本文とhashを台帳へ記録する。手入力pathを成功扱いするノードはない。
これらは生成エンジンの真正性を証明する仕組みではなく、接続された値を結果として扱うローカルな補助機能。
custom socketやJSON hashを認証境界とみなさない。

### Result / Session

画像は固定rootへcontent-addressed PNGとして保存。任意pathは入力にしない。
新Artifactはfingerprint=versioned RGB8画素・サイズのhash、file_sha256=PNG bytesのhash、value=file_sha256.png。
Resolveは両hash、サイズ、path境界を検証し、実IMAGEを返す。元画像のbindingではPlan時の全解像度fingerprintと照合する。
再Recordは保存済みファイルを検証して画素を比較し、同じなら以前のArtifactをそのまま返す。
旧Artifactにfile_sha256は付け足さず、旧byte hashによる読込と依存identityを保持。新形式を旧readerへ戻すdowngradeは非対応。
Text結果も本文hashを検証してSTRINGを返す。promptの自動補間とgenerator parameter適用は行わない。

sessions.pyは旧版のbounded root、OS-level lock、compare-and-swap、temp/fsync/replaceを維持。
v2用保存rootを分け、v1 sessionは明確なエラーで拒否する。画像ファイルとJSONは別ファイルであり、
両方を跨ぐtransactionや自動GCはない。Record後のsession保存失敗時は未参照PNGが残り得る。
冪等な再実行で同じPNGを再利用し、自動削除は行わない。

ComfyUI失敗時にはRecordを自動実行できないため、失敗監視を装わない。
開始台帳を先に保存し、必要なら別queueでRecord Failureを実行する。sessionを同時編集しない。
queue controller／panel／Timelineは未完の接続層ごと削除。将来の自動化は認証・worker永続化・job結果APIが確定してから別実装とする。

## Compatibility / validation

旧Planner ID・widget順・output順だけは維持し、旧plan_json内容の互換は提供しない。
Director v1とはnode ID/socket/schemaを分け、暗黙変換をしない。
開発用テストは公開ソースのtestsへ配置し、インストールZIPからは除外。実APIをモックし、STRING、schema、D&D backend、DAG、replan、hash、attempt、
冪等性、result binding、atomic session／競合を確認する。

参照: [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)、
[ComfyUI properties/cache](https://docs.comfy.org/custom-nodes/backend/server_overview)、
[ComfyUI datatypes](https://docs.comfy.org/custom-nodes/backend/datatypes)。
