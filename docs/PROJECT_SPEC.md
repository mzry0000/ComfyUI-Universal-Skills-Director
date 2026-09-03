# Project Specification

更新日: 2026-08-31

## 目的

ブラウザから選んだMarkdown／JSON Specificationを固定の制作規則として使い、ComfyUI／Floyoで
利用できるモデル非依存の計画と下流プロンプトを生成する。単発Plannerに加え、複数の
Scene、Shot、Variant、Stageを再開可能に管理する汎用Directorを提供する。

ノード内では最終メディアを生成しない。選択されたWork Itemの第1出力をplain Python
`str`かつComfyUI標準`STRING`として返す。image stageは既存GPT Image 2へ、他stageは
対応する下流ノードへ接続する。

## 標準Specification経路

- `USH_LoadSpecification`: browser D&Dで渡されたUTF-8 `.md`／`.json`のbasenameと本文を
  `USH_SPEC`へ正規化する。worker上のtrusted pathも互換fallbackとして受け付ける。
- `USH_PromptPlanner`: 1回のResponses API呼び出しで単発planと`final_prompt`を返す。

Hosted Skill Set、Hosted shell、Skill ID、upload/list、Skill allowlistは廃止済みである。

## Director経路

Directorは次の責務を分離する。

- Director Draft: OpenAIのstrict Structured Output。creativeなScene／Shot構造だけを含む。
- Director Plan: hostがID、revision、署名、依存関係を付与した不変の制作計画。
- Director Ledger: status、attempt、prompt ID、opaque output refを持つ可変の実行記録。
- Work Item: 1つのScene／Shot／Variant／Stageを選択した実行単位。
- Timeline Manifest: 完了したvideo／audio結果を時間順に渡す編集ソフト非依存JSON。

組み込みprofileは`generic`、`music_video`、`business_ad`、`ugc_ad`、`short_film`とする。
profileはplanning guidanceとnon-blocking semantic warningだけを提供する。制作ルールは既存
`USH_SPEC`で与え、profile専用Loaderやprofile固有top-level fieldは追加しない。

Work Itemは独立したschema fileを持たず、Planから決定論的に導出する。node境界では
plan ID、revision、plan／shot signatureとcomponent IDを元Planへ照合する。

## Directorノード

Categoryは`Universal Skills/Director`とする。

1. Plan Project: OpenAIでDraftを作り、決定論的Planと全項目`pending`の初期Ledgerを返す。
   `previous_plan`と`previous_ledger`を同時接続した場合はrevisionを1増やし、同じ
   `item_id`＋`shot_signature`の状態だけをreconcileする。
2. Validate Plan: schema、署名、参照、依存、Ledgerのstale状態を診断する。
3. Select Work Item: ready／exact／retry対象を1件選び、第1出力に`final_prompt: STRING`を返す。
   exact選択はscene／shot／variant／stageの4 IDをすべて要求する。
4. Record Result: 成功、失敗、選択などの結果をpure reducerでLedgerへ記録する。
5. Load Session: 固定session rootから安全にPlan／Ledgerを読む。
6. Save Session: optimistic revision付きでatomic保存する。
7. Timeline Manifest: Plan／Ledgerから中立manifestを生成する。

## OpenAI契約

- Responses APIを使用する。
- operator管理Specificationは`instructions`、request、context、画像は`input`へ分離する。
- 画像の実内容と期待roleが衝突する場合、実内容で利用可能な単一roleだけをPlanへ入れる。Plannerの
  field contractは不足参照の説明、生成後QA、proofをwarningsへ分類するよう求める。Specificationは
  画像に存在しない内容を存在させない。
- Plannerのfield contractは各事実を1つの担当fieldへだけ置く。`goal`は成果、`required_changes`は
  他fieldにない変更、`composition`は各構図要素、`text_elements`はexact copy、`preserve`はglobal
  continuity、`constraints`は生成時の禁止事項だけを持つ。
- DirectorのTarget Adapterはnamed fieldからlens、product action、dialogue、lyric moment、duration、
  fpsをstageに応じて渡す。`timing.start_seconds`と`workflow_hint`は実行・schedule情報としてPlanに残し、
  生成promptへ重ねない。選択Work Itemの`prompt`だけをstage-localな`goal`とし、project goalやsceneの
  progressionを再掲しない。audio／text stageにはvisual reference、composition、continuity、keyframe、
  camera／subject motionを渡さない。
- Director Draftは`text.format.type = json_schema`、`strict = true`で取得する。
- `store = false`とし、通常経路で`tools`を送らない。
- API keyは`OPENAI_API_KEY`または非共有`ush_config.json`から遅延取得し、node widget、workflow、payload、log、例外へ出さない。
- 画像用`final_prompt`は各入力画像の見た目を列挙する詳細preserve一覧をPlan JSONへ残し、goal、
  単一role、visual direction、exact text、実行時constraint、compactなglobal continuityを標準
  `STRING`へ出す。正しく分類されたwarningsは`STRING`へ出さず、hostはconstraint本文を意味解析して
  warningへ再分類しない。Target Adapterは担当field間のtrim後完全一致を1回へまとめ、画像用のglobal
  continuityは1つの`Continuity`行へ集約する。

## ComfyUI契約

- Floyo互換を優先してV1 Legacy Python node schemaを使う。
- `INPUT_TYPES`、`RETURN_TYPES`、`RETURN_NAMES`、`CATEGORY`、`FUNCTION`を明示する。
- custom socketは接続補助であり、security boundaryではない。node入口で必ず再検証する。
- browserは単一のclick／D&D領域で選ばれたfileをUTF-8として読み、`uploaded_filename`と`uploaded_content`の標準
  `STRING`へ埋め込む。workerへfileを書かず、専用upload routeを必要としない。
- clickとD&Dは同じcompactな領域へ統合し、独立したfile選択button、backing STRING editor、
  Specification本文previewは表示しない。読込後はfilename、workflow埋込状態、容量だけを表示する。
- Floyoが`WEB_DIRECTORY` JavaScriptを配信しない場合も、advanced STRINGへbasenameと本文を
  手動pasteできる。
- browserで読み込んだ本文はworkflow／prompt historyへ保存され、Planner実行時にOpenAIの`instructions`へ送る。
  UIと文書でこのdata boundaryを明示する。
- `spec_file`は任意のtrusted path fallbackとして維持し、ComfyUI inputまたはoperator設定rootの
  配下だけを読む。相対pathはこの優先順でrootを基準に解決する。
- D&Dとpathの同時指定を拒否する。D&Dでは`source_path = null`、pathではhost-only絶対
  `source_path`を再読込にだけ使い、OpenAI payload、Plan、sessionにはbasenameだけを渡す。
- 両経路でUTF-8、`.md`／`.json`、空本文、256 KiB上限、JSON objectとduplicate keyを検証する。
  path fallbackではさらにsymlink escape、hard link、secret設定fileを拒否し、1回のPlanner実行で
  diskから1回だけsnapshot化する。
- optional画像はComfyUI `IMAGE`の`[B,H,W,C]`を受ける。
- session node以外は外部ファイルを書き換えず、標準nodeはshellを実行しない。
- node実行中にComfyUIの`/prompt`へ再帰POSTしない。

## SessionとController

Session Storeは固定root、単純なsession名、symlink／traversal拒否、temp file、flush、fsync、
`os.replace`を使用する。revisionのread／compare／replaceはprocess内lockとOS-level
cross-process lockの同じcritical sectionで行う。Plan revisionとsession revisionは別管理とする。

外部queue controllerはoperator作成済みのstrict queue manifestだけを入力とし、default
previewとする。Session／Planからmanifestを生成せず、manifestのplan identityも実Planへ
照合しない。実送信にはexact selector、`--run`、`--confirm`を要求する。非loopback URLには
追加の明示許可を要求する。任意のbounded history pollingで非同期jobの完了を追跡できるが、
timeout時にjobをcancelしたとは扱わない。prompt本文やHTTP response bodyを監査表示へ出さない。

Timeline clipはScene相対のShot開始時刻を絶対timeline時刻へ変換し、source in、duration、
`speed = 1.0`、`sync_lock`、nullableで決定論的な`sync_group`、opaque asset refを持つ。

## 非目標

- メディア生成、GPU推論、動画編集そのもの
- 任意shell／script実行
- ComfyUI graphの自己queue
- Velorn projectへの直接書込
- GPL実装やprompt、UI文字列のコピー
- 第三者GPT Image 2ノード固有APIへの固定

## 配布条件

Python 3.10〜3.13、依存最小、secret／cache／runtime sessionを含まない
`ComfyUI-Universal-Skills-Director` folderとZIPを提供する。実ComfyUI、Floyo、第三者ノードとの
配線は対象環境で最終確認する。
