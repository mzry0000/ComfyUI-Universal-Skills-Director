# ComfyUI Universal Skills Director

Reusable Markdown/JSON Skills → concise prompts for existing ComfyUI generators.
v2は **Prompt Composerを中心にした小さな構成** です。画像自体は生成しません。

## 基本の使い方

1. **Universal Skills: Load Skill**へ、手元のUTF-8 .md／.jsonをD&Dします。同じ領域をクリックして選ぶこともできます。本文previewは表示しません。
2. **Universal Skills: Prompt Composer**へSkillを接続し、requestに今回作りたいものを書きます。
3. 参照画像があればimage1〜image4へ接続します。
4. final_promptを既存GPT Image 2等のprompt入力へ接続します。参照画像は生成ノードにも同じ順序で接続してください。

    Load Skill ──→ Prompt Composer ── final_prompt: STRING ──→ 既存の生成ノード
                       ↑                                      ↑
                  request・画像 ───── 同じ参照画像 ────────────┘

通常はこの2ノードだけで使えます。最終文を13項目のPlanへ分解して再構成する旧方式は廃止しました。
モデルが返すのはfinal_promptとwarningsだけです。warningsは最終文に混ぜません。

「画像で明白な見た目の再説明」は省きますが、「商品ラベルは変更禁止」のような明示条件は残す方針です。
文章を文字数で切り詰めたり、host側で制約を機械的に削ったりしません。品質・短さは実案件で確認してください。

### 入力と出力

| 入力 | 用途 |
| --- | --- |
| request | 今回の成果物と変更要求。例:「image1の商品を使った遠距離向けPOP什器のデザイン案を1枚。image2は売場の参考。商品ラベルは変更しない」 |
| target_profile | 下流の生成先。GPT Image 2ならgpt_image_2。画像生成モデルをこのノード内で呼び出す設定ではありません |
| generation_id | 同じ依頼で別案を作るときに数値を変更。通常のComfyUI cacheは維持します。再生成のseedではありません |
| context（advanced） | 任意の補足。旧target_notes／additional_contextを統合した欄。不要なら空欄 |
| model／reasoning_effort（advanced） | プロンプトを作るOpenAIモデルと推論設定。既定modelはgpt-5.6-luna（Luna）。Solを使う場合はgpt-5.6-solを指定 |
| image_detail（advanced） | OpenAIへ渡す参照画像のdetail |
| max_output_tokens（advanced） | 推論分も含むAPI出力上限。reasoning_effortとは独立。既定8192 |

Lunaの既定値は新規ノードに適用されます（旧Planner・任意のDirectorも共通）。
保存済みworkflowのmodel値は自動変更しません。既存ノードもLunaにする場合はmodel欄をgpt-5.6-lunaへ変更してください。

出力はfinal_promptとwarningsの標準STRINGです。
未接続のimage番号への言及はwarningsへ通知します（日本語に隣接するラベル・大文字にも対応）。文章自体は変更しません。
target_profileは生成先のラベルであり、専用の書式変換器ではありません。細かな書式・スタイルはSkillまたはrequestで指定してください。
画像入力はRGBのComfyUI IMAGE。各接続のbatchは先頭1枚を使用し、複数枚ならwarningを出します。
OpenAIへ渡すpreviewは最大辺2048pxへ縮小します。元の画像は変更しません。

## インストールとAPI key

ComfyUIのcustom_nodesへこのフォルダを置き、**ComfyUIが使用するPython**でrequirements.txtをインストールします。
Python 3.10以上、ComfyUI側のNumPy／Pillow／torchを使用します。既存のtorchをこのノードのために再インストールする必要はありません。

    python -m pip install -r requirements.txt

ush_config.example.jsonを同じフォルダへush_config.jsonという名前でコピーし、privateな設定ファイルとして編集してください。

    {
      "openai_api_key": "YOUR_OPENAI_API_KEY",
      "timeout_seconds": 120,
      "specification_roots": [],
      "enable_director": false
    }

設定後にComfyUIを再起動します。OPENAI_API_KEY環境変数が設定されていれば、そちらが優先です。
キーや設定ファイルをSkillに入れたり、workflowやGitHubへ公開したりしないでください。.gitignoreは配布に含めています。
このノードのAPI keyは、下流の画像生成ノードには転送されません。

### Floyo等のhosted環境

D&Dしたファイルはブラウザで読み、本文をworkflowへ埋め込みます。workerのフォルダ操作・upload先パス指定は不要です。
本文はworkflow／prompt historyに残り、Composer実行時に画像とともにOpenAIへ送信されます。Skillに秘密情報を入れないでください。
共有workflowのSkill内容も実行前に確認してください。

API keyのサーバー側設定は環境の提供者へ依頼してください。ノードにキーを直接埋め込む欄はありません。
提供側でこの拡張のJavaScript配信が有効である必要があります。配信されない場合はadvanced入力にfilename／contentを貼るfallbackがあります。
spec_fileは旧来のtrusted server path用のadvanced fallbackです。D&Dとの同時指定はできません。
path fallbackのみ、ComfyUI inputまたはoperator設定specification_roots配下を読みます。

## Director v2（任意）

複数案・複数工程が必要な場合のみ、ush_config.jsonのenable_directorをtrueにして再起動します。
無効時はDirectorをimportも登録もしません。通常の静止画1枚には不要です。

v2は **画像・テキスト工程を手動で選び、結果を保存して次へ進める補助機能** です。
動画／音声の工程実行、自動queue、失敗の自動監視、Timelineは含みません。
単発の動画用プロンプト作成には通常のPrompt Composerを使えます。

1. Plan ProjectでPlanとLedgerを作成し、Save Sessionで保存（初回expected_session_revision=-1）。
2. Load Session → Select Work Itemで工程を選択。最初はmode=next_ready、stage_idは空欄で構いません。
3. Selectのstarted_ledgerをSave Sessionで保存します。更新時はLoadからのsession_revisionを接続します。
4. 生成用workflowで再読込し、同じstage_idをmode=resumeで選択。final_promptを生成ノードへ接続します。
5. bindings_jsonに入力指定がある場合はResolve Image Input／Resolve Text Inputを使います。元画像を参照するslotには、Plan作成時の元画像も接続してください。
6. 生成IMAGEをRecord Image、生成STRINGをRecord Textへ接続し、Selectのticketとstarted_ledgerも接続します。Record後のledgerをSave Sessionで保存します。
7. 次回Load後、次の工程をnext_readyで選びます。

Select → Recordだけの同一workflowでも実行できますが、途中失敗に備えるなら上記の二段階保存を使ってください。
**同じsessionへ書く複数のSave Sessionを1回のqueueに置かないでください。** 保存競合はエラーになり、自動mergeはしません。

生成が失敗するとComfyUIは下流ノードを実行しません。その場合は保存済みのrunning工程をresumeし、
生成ノードを実行しない別workflowでRecord Failureへfailed／cancelledと理由を渡して保存します。
再試行はstage_idを明示してmode=retry。結果再送は同じattemptなら重複計上しません。
完了済み工程を作り直す場合は再計画でその工程のpromptまたはseed等のparameterを変更します。

Resolve Image Inputは保存した生成画像を実IMAGEへ戻します。
Resolve Text Inputは実際の生成文をSTRINGとして返します。text1等は自動展開されないので、
必要なら既存の文字列結合ノードでfinal_promptと組み合わせるか、生成ノードの別入力へ接続してください。
parameters_jsonも自動適用されません。生成ノードのsize／seed等へ手動で反映してください。

画像結果はComfyUI output/universal_skills_director_v2/resultsへ8-bit RGB PNGとして保存します。
新しい画像結果は画素の同一性とPNGファイルの完全性を別々のhashで確認します。
同じattemptへの同一画素の再Recordは、PNG圧縮条件が変わっても保存済み結果をそのまま再利用します。
既存v2の画像結果も読込・再Recordできます。更新後に作成した新形式の画像結果は、更新前のv2では読めません。
Record Imageは1画像／batch、最大64MP・128MiB、Record Textは最大32,000文字です。
Directorの元画像も各入力1画像／batchに限定します。複数画像のbatchは事前に分割してください。
台帳は同じv2フォルダのsessions配下へatomic保存します。
session JSONだけで画像ファイルを持ち運ぶことはできません。hosted workerの保存領域の永続性は提供者に確認してください。
同一sessionは単一operatorで運用してください。ComfyUIジョブの重複実行を排他制御するcontrollerではありません。

### 再計画

Load Sessionのplan／ledgerをPlan Projectのprevious_plan／previous_ledgerへ接続し、requestに変更要求を書きます。
Skillと必要な元画像も接続します。前のPlanをAPIへ渡し、既存工程のIDを保つよう指示します。
工程の並べ替えや説明変更だけなら完成結果を再利用し、prompt・参照・parameter・依存が変わると当該工程と下流を再生成対象にします。
モデルが意図せずpromptも変更した場合は再利用しません。再計画結果のJSONを確認してください。

## 旧版からの移行

- Load Specificationは表示名をLoad Skillへ変更。内部IDとD&Dの保存形式は同じです。
- 旧Prompt Plannerはdeprecatedな互換ノードとして残し、widget順と5つのSTRING出力順を維持します。
  plan_json出力の内容はschema_version=2.0のfinal_prompt／warningsへ変更しました。旧13項目を解析するworkflowは修正が必要です。
  新規workflowではPrompt Composerを使用してください。
- Director v2は新しいノードID／socket／Plan／Ledger形式です。**v1 Director workflow・sessionは自動変換しません。**
  旧sessionと生成物は旧版で開けるよう保管し、v2用Planを新しく作成してください。
- queue CLI、queueパネル、Timeline、用途別Python profile、旧schemaは廃止しました。制作分野のルールはSkillで指定します。

更新は旧フォルダへの上書きmergeではなく、ush_config.jsonと旧データをバックアップしたうえで、
custom_nodes外へ旧フォルダを退避し、新フォルダへ置換してください。旧版と新版を同時にcustom_nodesへ置かないでください。
これにより削除済みのJavaScript等が残ることを防ぎます。API keyはバックアップからprivate設定へ戻してください。

## 開発・仕様

公開ソースにはtests、最小の開発手順とオフラインCIを含めます。インストール用ZIPにはテスト／開発用資料／API key／runtimeを含めません。
ソース版での検証手順はdocs/DEVELOPMENT.mdを参照してください。CIにAPI keyは不要です。
モックテストで契約・状態遷移・保存処理を確認していますが、実ComfyUI／Floyoでの配線と実モデル品質は別途確認が必要です。

- [Architecture](docs/ARCHITECTURE.md)
- [Project specification](docs/PROJECT_SPEC.md)
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [ComfyUI node contract](https://docs.comfy.org/custom-nodes/backend/server_overview)

License: [MIT](LICENSE)
