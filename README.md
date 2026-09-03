# ComfyUI Universal Skills Director

ブラウザから選んだMarkdown／JSONの制作仕様を読み込み、OpenAI Responses APIで単発プロンプトまたは
複数Scene／Shotの制作計画を作るComfyUI向けカスタムノードです。
画像・動画・音声などの最終生成は対応する既存ノードへ任せます。

Directorの基本フロー:

```text
Load Specification ──USH_SPEC──► Director: Plan Project
                                      │
                 USH_DIRECTOR_PLAN + USH_DIRECTOR_LEDGER
                                      ▼
                           Director: Select Work Item
                         ┌────────────┴────────────┐
                         ▼                         ▼
             final_prompt: STRING       USH_DIRECTOR_WORK_ITEM
                         │                         │
              existing GPT Image 2         output_ref/status
                 or media node                    │
                         └────────────┬────────────┘
                                      ▼
                           Director: Record Result
                              ├─ Save Session
                              └─ Timeline Manifest
```

`Select Work Item`の第1出力`final_prompt`はplain Python `str`、ComfyUI型は標準
`STRING`です。image stageは既存GPT Image 2へ、video／audio／text stageは対応する
標準`STRING` prompt入力を持つ下流ノードへ接続します。
画像用STRINGはgoal、参照画像の単一role、変更／構図、正確な文字、実行時constraintsと
Plan全体のglobal continuityだけへ簡潔化します。各入力画像の見た目を列挙する詳細preserve一覧は
Plan JSONへ残します。Plannerのfield contractに従ってwarningsへ正しく分類された生成後QAやproofは、
STRINGへ混ぜません。
Plannerは各事実を`goal`、`required_changes`、`composition`、`text_elements`、`preserve`、
`constraints`の担当fieldへ1回だけ置きます。Target Adapterもtrim後の完全一致が複数fieldにある場合は
担当fieldを優先し、画像用のglobal continuityは`Continuity` 1行へまとめます。
Directorでは選択stageの指示だけを`Goal`にし、project goalやscene progressionを再掲しません。
audio／text stageにはvisual reference、visual composition、continuity、keyframe、subject／camera
motionを渡しません。

## ノード

### Universal Skills

- `Load Specification` — 単一領域のclick／D&Dで選んだ`.md`／`.json`本文を読み、`USH_SPEC`を返す。
  worker上のtrusted pathを指定する互換fallbackも利用できる。
- `Prompt Planner` — 1回のOpenAI呼び出しで単発planと`final_prompt: STRING`を返す。

### Universal Skills/Director

- `Director: Plan Project` — strict Director Draftを取得し、署名済みPlanと全Work Itemが
  `pending`の初期Ledgerを作る。既存Plan／Ledgerを同時接続するとrevisionを更新する。
- `Director: Validate Plan` — schema、署名、Ledgerのstale状態を診断する。
- `Director: Select Work Item` — `next_ready`、`exact`、`retry_failed`から1件を選ぶ。
  `exact`ではscene／shot／variant／stageの4 IDがすべて必要。
- `Director: Record Result` — status、attempt、opaque output refをLedgerへ記録する。
- `Director: Load Session` — 固定session directoryからsnapshotを読む。
- `Director: Save Session` — optimistic revision付きでsnapshotをatomic保存する。
- `Director: Timeline Manifest` — 完了resultから編集ソフト非依存JSONを作る。

Hosted Skill Set、Hosted shell、Skill ID、allowlist、upload/list toolは削除済みです。旧Hosted
workflowとの互換性はありません。

## インストール

1. ComfyUIを停止します。旧`ComfyUI-Universal-Skill-Host-Handoff` folderがある場合は、まず
   privateな`ush_config.json`を安全な場所へ退避してから旧folderを削除します。このfolderを
   `ComfyUI/custom_nodes/ComfyUI-Universal-Skills-Director`へ配置し、必要なら設定だけを戻します。
   旧folderと新folderを併存させると同じ`USH_*` node IDが二重登録されます。
2. ComfyUIのPython環境で依存を導入します。

```powershell
python -m pip install -r requirements.txt
```

3. `ush_config.example.json`を同じfolderの`ush_config.json`へコピーし、API keyを
   設定します。

```powershell
Copy-Item ush_config.example.json ush_config.json
```

```json
{
  "openai_api_key": "sk-...",
  "timeout_seconds": 120,
  "specification_roots": []
}
```

`ush_config.json`はカスタムノードfolder直下から自動で読み込まれます。設定後はComfyUIを
再起動してください。この実ファイルは平文の秘密情報を含むため、共有、workflow保存、
配布、commitをしないでください（`.gitignore`と配布除外の対象です）。
OpenAI公式は環境変数またはkey management serviceからの読込を推奨しています。本JSON方式は
ローカル／Floyo配置用の便宜機能なので、file ACLもoperatorだけが読めるようにしてください。

環境変数を使う場合は次のように起動できます。`OPENAI_API_KEY`が設定されている場合は、
安全な運用上の上書き手段として`ush_config.json`より常に優先されます。

```powershell
$env:OPENAI_API_KEY = "..."
Set-Location C:\path\to\ComfyUI
python main.py
```

`USH_TIMEOUT_SECONDS`または`ush_config.json`の`timeout_seconds`でOpenAI timeoutを変更
できます。`specification_roots`は後述のtrusted path fallbackを使う場合だけ設定します。

## SpecificationをclickまたはD&Dで読み込む

標準経路では、`Load Specification`ノードの小さなfile領域をclickしてPC上の`.md`／`.json`を
選ぶか、同じ領域へ1 fileだけD&Dします。独立した選択buttonはありません。読込後に表示するのは
filename、`Embedded`状態、容量だけで、本文previewと内部保存fieldは表示しません。ブラウザはfileを
UTF-8 textとして読み、basenameを`uploaded_filename`、本文を`uploaded_content`というbacking
ComfyUI `STRING`へ設定します。この2 fieldを標準操作で直接編集する必要はありません。
worker filesystemへの保存、worker pathの確認、専用upload routeは必要ありません。
ComfyUI frontendには汎用file upload inputがまだないため、画像用`image_upload`／`/upload/image`は
流用せず、公式JavaScript extension hookで追加した単一のclick／D&D領域だけがbrowser File APIを
使います。

受け付けるのはUTF-8の`.md`／`.json`、最大256 KiBです。JSONはobjectをrootにする必要があり、
duplicate keyや非JSON数値も拒否します。filename、拡張子、サイズ、空本文などはブラウザ側だけで
なくPython側でも再検証します。`ush_config.json`や`.env`をSpecificationとして読み込むことは
できません。

重要: browserで読み込んだ本文は、previewには表示しませんが`uploaded_content`としてworkflow JSONと
ComfyUI／Floyoのprompt historyに
保存されます。また、`Prompt Planner`または`Director: Plan Project`を実行するとoperator指示として
OpenAIへ送信されます。API key、password、未共有の機密文書を読み込ませないでください。

Floyoがカスタムノードの`WEB_DIRECTORY` JavaScriptを配信しない環境ではclick／D&D領域が表示されない
可能性があります。その場合もadvanced入力を開き、`spec_file`を空のまま、basenameを
`uploaded_filename`、file本文を`uploaded_content`へ手動pasteすれば同じin-memory経路を使えます。
両fieldは必ずセットで指定します。

### Trusted path fallback

worker上のpathをoperatorが把握している環境だけ、advanced入力を開き、従来の`spec_file`を任意のfallbackとして
利用できます。この場合は`uploaded_filename`と`uploaded_content`を空にし、許可root内にある
`.md`／`.json`の絶対pathまたは相対pathを`spec_file`へ入力します。browser入力とpath入力の同時指定は
曖昧さを避けるため拒否されます。

相対pathは次の信頼rootを優先順に検索します。

- ComfyUI input directory
- `ush_config.json`の`specification_roots`でoperatorが許可したdirectory

`specification_roots`の相対pathはカスタムノードfolderを基準にします。配下の
subdirectoryは利用できますが、symlink escape、許可root外、UNC／Windows device path、
`ush_config.json`、`.env`、hard link、非UTF-8、空file、256 KiB超のfileは拒否します。
許可rootはoperatorが管理し、意図したSpecification以外を作成できる第三者の書込先を指定しないで
ください。このpath経路はD&Dに必要な条件ではなく、Floyo workerのfolderへアクセスできない
通常利用では設定不要です。

input directoryまたは設定rootの直下ならbasename、subdirectoryなら
`project-a/rules.md`のように指定できます。同名fileが複数rootにある場合は先に挙げたrootが
優先されます。既存workflowとの互換用に入力名`spec_file`とnode IDは維持しています。

Loaderは内容をJSON-safeな`USH_SPEC`へ正規化します。browser-embedded／manual paste経路の`source_path`は
`null`で、Plannerは検証済みのin-memory snapshotを使います。trusted path経路だけはPlanner実行時に
host-onlyの`source_path`を信頼rootへ再照合してdiskから再読込します。絶対pathはOpenAI、Plan、
sessionへ渡さず、どちらの経路も公開metadataにはbasenameの`source_file`だけを含めます。

## Prompt Plannerを使う

Specificationは複数案件で再利用する固定制作規則、`request`は今回だけの制作条件です。
`.md`または`.json`を`Load Specification`へD&Dし、その出力と必要な画像を`Prompt Planner`へ
接続します。第1出力`final_prompt`を画像生成ノードなどの標準`STRING` prompt入力へ接続します。

```text
Load Specification.specification ─► Prompt Planner.specification
Load Image ──────────────────────┬─► Prompt Planner.image1
                                └─► downstream image reference
Prompt Planner.final_prompt ──────► downstream prompt
```

画像はPlannerから下流へ自動転送されません。各`Load Image`出力をPlannerと下流ノードの対応する
reference／edit画像入力へ分岐してください。

## Directorを使う

1. `Load Specification`を`Plan Project`へ接続する。
2. request、`director_profile`、下流の`target_profile`を選ぶ。
3. 必要なら`image1`〜`image4`を接続する。
4. PlanとLedgerを`Select Work Item`へ接続し、通常は`next_ready`を選ぶ。
5. 第1出力`final_prompt`を選択stageに対応する下流ノードへ接続する（image stageならGPT Image 2など）。
6. 生成物を保存した後、その安定した参照文字列を`Record Result`の`output_ref`へ渡す。
7. 更新Ledgerを`Save Session`で保存し、次回`Load Session`から再開する。

再計画では、前回のPlanとLedgerを`Plan Project`の`previous_plan`／`previous_ledger`へ必ず
セットで接続します。同じbriefのPlanだけを受け付け、revisionを1増やします。新旧で
`item_id`と`shot_signature`がともに一致するWork Itemだけが状態とreceiptを引き継ぎ、
変更されたWork Itemは`pending`へ戻ります。

組み込みprofile:

- `generic` — 汎用のScene／Shot分解
- `music_video` — 音楽・歌詞timingとcoverage
- `business_ad` — hook、demonstration、CTA、brand fact保持
- `ugc_ad` — creator dialogueと自然な商品handling
- `short_film` — cast、wardrobe、prop、screen directionの連続性

profileは共通schemaへguidanceとwarningを重ねます。用途固有の制作規則は別Loaderを増やさず、
選択した`USH_SPEC`へ記述します。

## Plan、Ledger、Session

Planはcreative documentで、ID、revision、plan/shot signatureを持ちます。Ledgerは別documentで、
各Shot × Variant × Stageのstatus、attempt、prompt ID、workflow ID、output refsを保持します。
古いPlanのWork ItemやLedgerはsignatureでstaleとして拒否されます。

`USH_DIRECTOR_WORK_ITEM`は独立したJSON Schemaを持ちません。Director Planから決定論的に
導出し、Record Resultなどの入口でplan ID、revision、plan／shot signatureと4つのcomponent
IDを元Planへ照合します。

`output_ref`は文字列として保存するだけで、nodeはそのpathやURLを開きません。Session保存は
単純なJSON filenameだけを許可し、path traversal／symlinkを拒否します。更新には直前の
`session_revision`が必要で、process内lockとOS-level cross-process lockの内側でtemp file、
fsync、`os.replace`を使います。

保存先は通常`ComfyUI/output/universal_skills_director/sessions/`です。ComfyUIの
`folder_paths`をimportできない単体実行時だけpackage内`runtime/`へfallbackします。

## Timeline Manifest

Timeline nodeは成功、選択、またはassembled済みのvideo／audio resultから、stable track／clip
ID、開始秒、source in、duration、`speed = 1.0`、`sync_lock`、nullableな決定論的
`sync_group`、opaque asset refを持つversion付きJSONを返します。Shotの明示
`start_seconds`はScene開始時刻からの相対値としてtimeline上の絶対時刻へ変換します。
実mediaをprobe／編集しません。
Velornなどへのimporterはこの中立manifestを境界として別実装できます。

## 外部queue controller

custom node自身はComfyUIの`/prompt`を呼びません。任意の外部CLIは、operatorが事前に作った
ComfyUI API-format graph入りのstrict queue manifestを検証し、既定ではprompt本文を伏せた
previewだけを表示します。CLIはSessionやPlanからmanifestを生成せず、manifest内の
`plan_id`／`plan_revision`が実在するPlanと一致するかも照合しません。これらはoperatorが
確認する監査metadataです。

最小manifest例です。`prompt`にはComfyUIの「Save (API Format)」相当のgraphを入れ、例の
class typeとinputは実際のworkflowへ置き換えてください。

```json
{
  "schema_version": "1.0",
  "queue_id": "queue_demo_001",
  "plan_id": "dir_0123456789abcdef0123",
  "plan_revision": 1,
  "allowed_selectors": [
    "scene-001/shot-001/variant-001/keyframe"
  ],
  "items": [
    {
      "selector": "scene-001/shot-001/variant-001/keyframe",
      "work_item_id": "scene-001-shot-001:scene-001-shot-001-variant-001:keyframe",
      "prompt": {
        "10": {
          "class_type": "ExistingGPTImage2Node",
          "inputs": {
            "prompt": "Replace with Select Work Item final_prompt"
          }
        }
      }
    }
  ]
}
```

以下のコマンドはnode package directoryから実行します。

```powershell
python tools/director_queue.py director-queue.json
python tools/director_queue.py director-queue.json --selector 'scene-001/shot-001/variant-001/keyframe'
python tools/director_queue.py director-queue.json --selector 'scene-001/shot-001/variant-001/keyframe' --run --confirm
python tools/director_queue.py director-queue.json --selector 'scene-001/shot-001/variant-001/keyframe' --run --confirm --wait
```

実送信はmanifest allowlistのexact selector、`--run`と`--confirm`の同時指定が必要です。
`--timeout`は0より大きく300秒以下です。remote
ComfyUIにはさらに`--allow-remote`が必要です。一度に選べるのは最大32件です。`--wait`は
GET `/history/{prompt_id}`をbounded pollし、既定2秒間隔、各job最大600秒です。範囲は
`--poll-interval`が0.1〜60秒、`--wait-timeout`が1〜3600秒です。wait timeoutになっても
ComfyUI側の非同期jobはcancelされません。Director sidebar panelもbrowser内の簡易previewと
CLI command copyだけを行い、HTTP送信やnode hookは行いません。panelで読めてもCLIのstrict
validationで拒否される場合があり、実行可否についてはCLIを最終的な権威とします。

## 公式仕様の確認記録

2026-08-27に以下の公式資料を確認しました。

- OpenAI: [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)、
  [Responsesへの移行](https://developers.openai.com/api/docs/guides/migrate-to-responses)、
  [GPT-5.6 Sol model](https://developers.openai.com/api/docs/models/gpt-5.6-sol)、
  [API認証](https://developers.openai.com/api/reference/overview)
- ComfyUI: [V1 node properties](https://docs.comfy.org/custom-nodes/backend/server_overview)、
  [datatypes](https://docs.comfy.org/custom-nodes/backend/datatypes)、
  [flexible/custom inputs](https://docs.comfy.org/custom-nodes/backend/more_on_inputs)、
  [JavaScript extensions](https://docs.comfy.org/custom-nodes/js/javascript_overview)、
  [JavaScript hooks](https://docs.comfy.org/custom-nodes/js/javascript_hooks)、
  [widget objects](https://docs.comfy.org/custom-nodes/js/javascript_objects_and_hijacking)、
  [generic file upload issue](https://github.com/Comfy-Org/ComfyUI_frontend/issues/3461)、
  [server routes](https://docs.comfy.org/development/comfyui-server/comms_routes)

## セキュリティ境界

- API keyをnode widget、workflow、payload、log、例外へ含めない。
- operator Specificationを`instructions`、request/context/imageを`input`へ分離する。
- OpenAIにはstrict JSON Schema、`store: false`、tool-free payloadを送る。
- shell、Specification内script、任意commandを実行しない。
- custom socketは接続補助とし、すべてのnode入口でschemaを再検証する。
- browserで読み込んだSpecificationはworkerへ保存せず、専用backend upload routeも追加しない。
- browserで読み込んだ本文はworkflow／historyへ保存され、Planner実行時にOpenAIへ送信されるため、秘密情報を
  Specificationへ含めない。
- Base64画像、Specification本文、prompt、HTTP error bodyを監査出力へ漏らさない。

## 互換性確認

実OpenAI model権限、Floyo審査、ComfyUI上のload、第三者生成ノードとの実配線は、導入先の
環境で確認してください。
