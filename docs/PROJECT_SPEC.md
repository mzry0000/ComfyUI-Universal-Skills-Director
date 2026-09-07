# Project Specification v2

更新: 2026-09-07

## 目的

再利用する制作規則（Skill）と今回の依頼・参照画像を、既存の生成ノードへ渡せる簡潔なpromptへ変換する。
標準利用はLoad SkillとPrompt Composerの2ノード。画像生成自体は下流ノードが行う。

## 必須契約

- UTF-8 .md／.jsonをbrowserで読み、workflowへfilename/contentを埋め込む。worker pathは不要。
- clickとD&Dは同じ領域を使用し、独立upload buttonや本文previewを表示しない。
- Skillは最大256KiB。JSONはobjectのみ、重複keyや非JSON数値を拒否。secret設定fileは読込対象外。
- trusted path fallbackはComfyUI inputかoperator指定root配下のみ。D&Dとの同時指定、traversal、symlink escape等を拒否。
- requestは20,000文字、contextは40,000文字まで。最大4画像、各batchの先頭を使用。
- OpenAI Responsesのstrict Structured Outputsでfinal_promptとwarningsを取得。
- PydanticからAPI schemaを投影し、同じ定義でhost検証。送信時のみminLength/maxLengthと不要な注釈を除き、hostの文字数制約・pattern・数値／配列制約は維持する。形式を満たしても品質保証とはみなさない。
- final_promptは非空・最大32,000文字の標準Python str／ComfyUI STRING。文章を組み直したり切り捨てたりしない。
- 明示条件・exact copyは残す。参照画像の見た目の繰返し、重複する定型見出し、生成後QAは避ける。
- warningsは別STRING。拒否／未完了／不正応答を成功として返さない。
- 未接続・範囲外のimageラベルは日本語・大文字もwarning対象。Directorは各Stageのlocal slotで照合。診断に未知キー名・入力値を出さない。
- API keyは環境変数優先、固定private ush_config.jsonがfallback。workflow、log、payload本文にkeyを載せない。
- store=false、toolsなし。max_output_tokensとreasoning_effortは別設定。
- 通常のComfyUI cacheを尊重し、generation_idで明示的に別案を要求する。cacheの永続性やAPI呼出し回数の絶対保証はしない。

## 任意Director

- enable_director=trueでのみimport／登録する。
- v2は画像・テキスト工程の手動計画／選択／結果記録／再開まで。
- Stage固有prompt、media kind、target profile、input binding、parameterを持つ。用途別profileはSkillで表現。
- 新Stage IDはhost発行。表示orderとIDを独立させ、previous Planをモデルへ渡して変更要求を適用する。
- project IDはbriefから独立。revisionとsignatureをhostが管理する。
- 実行要素と説明用metadataを区別してhashを作り、不要な再生成を避ける。
- PlanとLedgerを分離し、attempt開始と結果通知を別処理にする。terminalイベントは冪等。
- 成功は接続された実IMAGE／STRINGから記録。画像を固定rootへ保存し、依存結果を実IMAGE／STRINGとして解決する。
- 新規画像結果はRGB8画素fingerprintとPNGのfile_sha256を分離。旧v2 Artifactの読込・再Recordは元の形とhashを保持し、新形式への暗黙移行を行わない。
- generator parameterとテキスト結合は利用者が接続／反映する。自動graph構築や自己queueはしない。
- 失敗の自動監視はしない。開始状態を保存し、別queueで手動失敗記録できる。
- sessionは固定root、OS-level lock、expected revisionによる競合拒否、atomic replace。
- v1 sessionは変換・上書きしない。v2は別rootと別socket形式。

## 非目標

メディア生成、動画／音声工程の実行、自動queue、Timeline／編集、任意shell／script、Velorn projectへの直接書込。
画像の自動最適化や生成品質の保証も行わない。実ComfyUI/Floyo、認証、worker永続性は環境別に確認する。

## 配布

README、最小の実装と運用文書、requirements、private configの空template、MIT LICENSE、.gitignore。
公開ソースには開発テストと最小の開発契約・CIを併置。インストール用ZIPは許可リスト方式で作り、開発テスト、サンプル、旧実装、API key、cache、runtime session、生成画像を含めない。
旧データの自動削除やGitHubへの自動公開は行わない。
