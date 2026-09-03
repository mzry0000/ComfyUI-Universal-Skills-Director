# Architecture

更新日: 2026-08-31

## 全体フロー

```text
D&D .md|json ─► browser UTF-8 read ─┐
                                    ├─► Load Specification ─► USH_SPEC
trusted path fallback ──────────────┘                          │
request + optional IMAGE ─────────────────────────────────────┤
                                                             ▼
                                                      Director Planner
                         OpenAI strict Director Draft (1 call)
                                                             │
                         deterministic hydrate / validate
                                                             ▼
                       USH_DIRECTOR_PLAN + USH_DIRECTOR_LEDGER
                                                             │
                           Select Work Item / retry gate
                          ┌──────────────┴──────────────┐
                          ▼                             ▼
                final_prompt: STRING         USH_DIRECTOR_WORK_ITEM
                          │                             │
                 existing media node          opaque result reference
                          └──────────────┬──────────────┘
                                         ▼
                                 Record Result
                                  ┌──────┴──────┐
                                  ▼             ▼
                              Save Session  Timeline Manifest
```

## Layering

### ComfyUI wrapper

`nodes.py`は単発Specification node、`director_nodes.py`はDirector nodeだけを定義する。
どちらも薄いadapterとし、OpenAI SDK、ComfyUI、filesystemの詳細をcoreへ混ぜない。
`web/js/specification_drop.js`はbrowserのlocal fileをUTF-8 textとして読み、basenameと本文を
標準`STRING` widgetへ設定する。worker storageへ書かず、専用server routeも追加しない。

### Specification

`specification.py`はbrowserから受け取った`uploaded_filename`／`uploaded_content`を標準経路として
in-memoryで再検証する。UTF-8、最大256 KiB、`.md`／`.json`、安全なbasename、空本文、
JSON object、duplicate keyをfail closedにし、`source_path = null`のsnapshotを作る。この本文は
workflow／prompt historyに保持され、Planner実行時にOpenAIへoperator instructionとして送られる。

任意のpath fallbackでは明示pathをComfyUI inputまたはoperator設定rootの内側へ
制限し、相対pathをこのroot順に解決する。実体path、symlink escape、hard linkも検証する。
path-backed `USH_SPEC`だけはhost-onlyの`source_path`から再解決し、workflow内dictの本文を
信用しない。Planner入口でdisk snapshotを1回作り、payload、fingerprint、Planを同じ内容から
構築する。両経路とも公開`source_file`はbasenameだけで、絶対pathをOpenAI／Plan／sessionへ
渡さない。D&Dとpathを同時指定した入力は拒否する。

### OpenAI planner

`director_planner.py`は入力制限、画像encode、instruction/data分離、Responses payload、
mock可能client境界だけを担当する。モデルはDirector Draftを返し、host管理値を作らない。

### Director core

`director_core.py`はDraftのNFC正規化、安定sort、ID生成、revision、canonical JSON、SHA-256、
参照／order／timing／dependency検証、Work Item flatten／selectを純粋関数で行う。
同一の意味的入力は同一IDとsignatureを生成する。

初回hydrateはrevision 1とする。同じbriefの`previous_plan`を渡した再計画はrevisionを1増やす。
`director_state.py`のreconcileは、新旧で`item_id`と`shot_signature`が両方一致するWork Itemの
state／receiptだけを新Ledgerへ移し、それ以外を`pending`で作り直す。exact selectionは
scene／shot／variant／stageの4 IDが揃った場合だけ行う。

### ProfileとTarget Adapter

`director_profiles.py`は共通schemaへ縦用途別guidanceとsemantic warningを重ねる。
`target_adapters.py`は選択Work Itemを既存のモデル非依存planへ写像し、最終的な
`final_prompt`を作る。モデルが作った詳細はPlan（単発の`downstream_prompt`またはDirectorの
Shot field）へ保持し、最終STRINGではgoalと構造化済み要件から簡潔に再構成する。画像profileへ
motionおよび各入力画像の見た目を列挙する詳細preserve一覧を出力せず、参照の単一role、実行時
constraints、Plan全体のglobal continuityだけを簡潔に渡す。Plannerのfield contractは生成後QA、
proof、将来条件をwarningsへ分類するよう求め、正しく分類されたwarningsをadapterはSTRINGへ出さない。
同じcontractは`goal`、`required_changes`、`composition`、`text_elements`、`preserve`、`constraints`
の担当範囲を分け、1つの事実を1 fieldだけへ置く。adapterはconstraints本文を意味解析して再分類しない。
担当field間の重複除去はtrim後の完全一致値だけに限定し、曖昧な意味判定や表記の書換えは行わない。
画像用global continuityは1つの`Continuity`行へまとめる。GPT Image 2固有の情報をDirector schemaへ
埋め込まない。

Director Work Itemのnamed fieldはTarget Adapterがstage別に写像する。image／videoへlensと
product action、video／audio／textへdialogueとlyric momentを渡す。durationはvideo／audioだけの
timingへ、fpsはvideoだけへ渡し、`start_seconds`はTimeline Manifest用のschedule情報としてpromptへ出さない。
選択stageの`prompt`だけを実行用`goal`とし、project goalとscene progressionはPlan JSONに残す。
audio／textではvisual reference、composition、continuity、keyframe、subject／camera motionを除外する。
これにより`shot.prompt`はsubject、setting、named fieldにないstage instructionだけを持てる。

### State、Session、Timeline

`director_state.py`はPlanと分離したLedger reducerを提供する。Plan signatureとshot
signatureが一致しないitemはstaleとして扱う。receipt履歴を再生してitemのtransition、attempt、
output、errorと照合し、Ledger本体だけの直接書換えを拒否する。resultはpathを開かないopaque
stringである。

`director_sessions.py`は固定rootのversion付きsnapshotだけをatomic保存する。process内lockと
OS-level cross-process lockの内側でrevisionのread／compare／replaceを行い、
`expected_session_revision`が古い更新を拒否してsilent overwriteしない。

`director_timeline.py`は成功／選択／assembled済みresultだけをstable clipへ変換する。
Shotの`start_seconds`はScene開始時刻からの相対値で、manifestでは絶対timeline時刻になる。
clipは`speed = 1.0`、`sync_lock`、nullableで決定論的な`sync_group`を持つ。実ファイルの存在や
codecは検査せず、欠損はwarningへ出す。

### External controller and UI

`web/js/specification_drop.js`はnode入力の補助だけを行い、HTTP uploadやbackend routeを持たない。
Floyoが`WEB_DIRECTORY` JavaScriptを配信しない場合は、advanced STRINGへbasenameと本文を
手動pasteする同じbackend契約へfallbackする。

`director_controller.py`はoperatorが事前構築したComfy API-format graphのstrict manifest
validationとredacted previewを提供する。SessionやPlanからmanifestを生成せず、manifestの
plan identityを実Planへ照合しない。HTTP送信は`tools/director_queue.py`だけが担い、defaultは
dry-runである。`web/js/director_panel.js`はsidebar上の簡易previewとCLI command copyのみを
提供し、fetch、backend route、node hook、monkey patchを持たない。strict validationと実行可否の
最終権威はCLIである。

## Authority boundary

```text
OpenAI: title / goal / scene / shot / prompt draft / creative alternatives
Host:   IDs / revision / signatures / normalization / dependencies / selection
Ledger: attempt / status / receipt / prompt_id / workflow_id / output_refs
User:   Specification / request / profile / approvals / exact queue selector
```

この分離により、モデルがstatusを偽造したり、古い生成結果を新Planへ混ぜたりすることを
防ぐ。

## Schema policy

- `schemas/plan.schema.json`: 単発Plannerのruntime/API schema。
- `schemas/director_draft.schema.json`: OpenAI strict output専用。
- `schemas/director_plan.schema.json`: host hydrate後のDirector Plan。
- `schemas/director_ledger.schema.json`: 可変runtime state。
- `schemas/timeline_manifest.schema.json`: 編集handoff。

全objectは全property requiredかつ`additionalProperties: false`とする。nullが必要なfieldは
型unionで明示する。schema validatorが未対応のkeywordは使用しない。
`USH_DIRECTOR_WORK_ITEM`は独立schemaを持たず、Director Planから導出し、境界でPlanのidentity、
signature、component IDへ照合する。

## Failure policy

不正schema、重複ID、参照不整合、cycle、stale、revision conflict、unsafe pathはfail closed
とする。profile上の品質不足やtimelineの未生成結果はnon-blocking warningとする。例外には
API key、prompt本文、画像data URL、Specification本文、HTTP bodyを含めない。

## Velorn境界

Velorn調査から採用するのはPlanとLedgerの分離、段階的Work Item、timeline handoffという
抽象構造だけである。実装はclean-roomで、GPL source、内部名、prompt、UI、workflow IDを
コピーしない。
