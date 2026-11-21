# kioku 向け参照用要約（reference_summary）と要約入力の切り詰め修正

- 日付: 2026-11-22
- 状態: 実装済み（v0.14.5 / v0.15.0）。接続先と認証の方針は 2026-11-22 に確定
- 関連: `app/ai/processor.py`、`app/ai/summarizer.py`、`app/services/background_processor.py`、`app/routers/exports.py`

## 目的

kioku（長期記憶）が Saved の記事を検索で引けるようにする。kioku は
`GET /api/export/saved-articles` を毎晩丸ごと読み、記事ごとの本文として検索に使う。
今ある `ai_summary` は人が一覧で読む用で、固有名詞・型番・数値などの語彙検索の手がかりが
足りない（中央値 274 字）。引くための別の要約 `reference_summary` を足す。

あわせて、要約の入力が先頭 3,000 字で切られていて結論が落ちる問題を直す。

## 実測した現状（2026-11-22、本番 DB）

- 記事 17,775 件、Saved 2,061 件（本文あり 1,876 件、なし 185 件）。
- 本文あり 17,096 件のうち 3,000 字超 8,927 件（52%）、6,000 字超 5,932 件、12,000 字超 2,590 件。
  本文の中央値（Saved）は約 6,600 字。
- 切り詰め箇所: `processor.py:95`（要約+タグ。本番の経路）と `summarizer.py:36`（手動の要約）が
  `text[:3000]`。`question_suggester.py` が 2,000 / 1,500 字、`tagger.py` が 500 字。
- LLM は lethe の Ollama（`qwen3.8:27b`）。context は 8,192 トークンで、超過はエラーにならず
  先頭側が黙って捨てられる。`OLLAMA_CONTEXT_LENGTH` を 32,768 に上げる作業は lethe 側で実施済み
  （反映の確認は実装時に行う。「検証」参照）。

## 目標

1. 要約の入力を、結論を含む範囲まで渡す（上限の引き上げ＋長文は先頭と末尾を残す）。
2. Saved の記事ごとに、検索用の `reference_summary`（600〜1,000 字、長くても 1,200 字）を作る。
3. export の JSON にその欄を足し、kioku が毎晩全件を読む形を保つ。

## 非目標

- 差分 API・記事 ID 指定の取得 API は作らない（kioku は毎晩全件を読み、「前の晩にあって今夜に無い」で
  Saved 解除を判断する）。
- Saved 以外の記事に `reference_summary` を作らない。
- 既存の `ai_summary` / `tag_suggestions` / `genre` の仕様は変えない。`question_suggester` と
  `tagger` の切り詰めは今回扱わない。
- 17,775 件の既存 `ai_summary` の再生成はしない（`reference_summary` が kioku の入口になるため）。
- export の認証（Bearer 鍵）は作らない（「決定事項」）。

## アンチゴール

- 本番のタグ品質（ジャンル分類）を巻き込んで悪化させること。要約とタグの combined call には
  手を入れず、参照用要約は別の LLM 呼び出しにする。
- 前の晩と同じ内容の記事が、再生成のたびに別内容として kioku に入り直すこと
  （`reference_summary` は、版が変わらない限り作り直さない）。
- 生成が滞ったとき、他のフェーズ（抽出・要約・タグ）の進みを止めること。

## 制約

- 記事テーブルの UPDATE は、どの列でも FTS トリガーが走り本文を再インデックスする。記事 1 件に
  つき UPDATE は生成 1 回ぶんだけにし、一括 UPDATE はしない（2,061 件を 1 件ずつなら問題ない）。
- 追加列は `main.py` の手動 `ALTER TABLE` で足す（`create_all` は既存テーブルを変えない）。
- テストは LLM をモックする。
- 本番は launchd 管理。反映は `make deploy` と `bootout` / `bootstrap`
  （`kickstart -k` だけでは plist の環境変数は再読込されない）。

## 選んだ方針（Approaches）

### 参照用要約の作り方

- **A（採用）: 別の LLM 呼び出し＋別のバックグラウンドループ。** 専用プロンプト、専用レーン
  （`reserved`）、専用のバックオフ。combined call（要約+タグ）は変えない。
  トレードオフ: LLM 呼び出しが増える。ただし対象は Saved のみで、新しい Saved から順に作る。
- B: combined call に参照用要約を相乗りさせる。呼び出しは増えないが、出力形式が複雑になり、
  タグ品質の根拠（SUMMARY を書いた直後にタグを継続する形）を壊すおそれがある。却下。
- C: kioku 側で本文から作る。SnoReader の変更は小さいが、本文の取得経路が kioku 側に要り、
  本文が取れていない記事の扱い（抜粋由来の印）が SnoReader の知識になる。却下。

### 保存先

- **採用: `articles` に列を足す**（`ai_summary` / `chat_suggestions` と同じ慣習）。
  トレードオフ: 版を上げて作り直すと、Saved 2,061 件ぶんの UPDATE と FTS 再インデックスが走る
  （1 件ずつ、数十秒ぶんの負荷で済む）。
- 別テーブルにすれば FTS の再インデックスは避けられるが、1:1 の結合が増え、既存の慣習から外れる。却下。

### 入力の切り詰め

- **採用: 上限を定数化して引き上げ、超えたら先頭と末尾を残す。** 結論は末尾にあることが多いため。
  トレードオフ: 中間の事実が落ちる。参照用要約は上限が大きく、落ちるのは 16,000 字を超える
  少数の記事だけ。
- 上限だけ上げる案: 12,000 字を超える 2,590 件で結論が落ち続ける。却下。

## 設計

### 1. 要約入力の修正（先に別ブランチ・別リリース）

- 定数: `SUMMARY_INPUT_CHARS = 12_000`（`app/ai/summary_rules.py`）。`processor.py` と
  `summarizer.py` の `text[:3000]` を、共有のヘルパー `clip_for_llm(text, limit)` に置き換える。
- `clip_for_llm`: `len(text) <= limit` はそのまま。超えたら先頭 `limit * 2 // 3` 字と末尾
  `limit // 3` 字を `"\n[…中略…]\n"` でつなぐ。
- 12,000 字 ≒ 約 16,000 トークンで、context 32,768 に出力 1,792 とシステムプロンプトを足しても収まる。
- 既存の `ai_summary` は再生成しない。直したあとに作られる分から効く。

### 2. 参照用要約

#### 列（`articles`）

| 列 | 型 | 中身 |
|---|---|---|
| `reference_summary` | TEXT | 要約本文。未生成は NULL |
| `reference_summary_at` | TEXT | 生成日時（UTC ISO 8601） |
| `reference_summary_version` | TEXT | `<model>/ref-v<N>` の形（例 `qwen3.8:27b/ref-v1`） |
| `reference_basis` | TEXT | `content`（本文）または `excerpt`（フィードの抜粋） |

#### 生成

- 入力: `extract_status` が NULL で `content` がある記事は `content` を、そうでなければ
  `summary`（フィードの抜粋）を使う（`reference_basis` に記録）。どちらも空なら対象外にして
  `reference_summary` は NULL のまま。
- 入力の上限は `REFERENCE_INPUT_CHARS = 16_000`（`clip_for_llm` を使う）。context 32,768 に対し、
  出力の `max_tokens` は 1,536。
- プロンプトは人が読むためでなく、引くための要約。日本語で、見出し付きの 4 部構成:
  `【要旨】`（1〜2 文）、`【事実】`（箇条で 5〜10 個。固有名詞・版・型番・数値・日付を原文の表記のまま。
  英語の名前は英語のまま）、`【主張と出どころ】`（記事の主張／公式の発表／推測を区別）、
  `【時点】`（公開日、または「〜の版で」）。
- 出力の検証: 4 つの見出しが揃い、全体が 300〜1,500 字。外れたら保存せず、次の周期で再試行する
  （同じ記事で空回りしないよう、失敗は専用のバックオフ辞書 `_reference_skip_until` で 300 秒）。
- `reference_summary_version` は、モデル名（`SNOREADER_LLM_MODEL`）とプロンプトの版
  `REFERENCE_PROMPT_VERSION = 1` から作る。

#### 実行の順序（新しいバックグラウンドループ）

- Phase 4: 1 ループ、`reserved` レーン、フォアグラウンドより低い優先度（`PRIORITY_BACKGROUND`）。
  Phase 0〜3 のバックオフとは別の辞書を使い、片方の失敗がもう片方を止めない。
- 対象: `is_saved` かつ `reference_summary IS NULL`、`saved_at DESC`（新しく Saved にした記事から先）。
  そのあと、`reference_summary_version` の**プロンプトの版**が現行より古い記事（`ref-vN` の N が古い）を
  古いものから作り直す。モデル名の違いだけでは作り直さない（モデルを切り替えたときに
  2,061 件の再生成が意図せず始まるのを防ぐため）。
- 抜粋由来（`excerpt`）の記事が、あとから本文が取れて `extract_status` が NULL になった場合は、
  `content` 由来で作り直す。
- 1 件につき UPDATE は 1 回（4 列まとめて）。
- 参考: 1 件 30〜60 秒とすると Saved 全件で 17〜34 時間。新しい記事から先に済むので途中でも使える。

### 3. export

`GET /api/export/saved-articles` の各要素に次の 5 欄を足す。既存の欄は変えない。

```json
{
  "reference_summary": "…または null",
  "reference_summary_at": "2026-11-22T12:34:56+00:00",
  "reference_summary_version": "qwen3.8:27b/ref-v1",
  "reference_basis": "content",
  "genre": "ai_misc"
}
```

- 未生成の記事は、`reference_summary` / `_at` / `_version` / `_basis` をすべて `null` で返す
  （kioku はこのときだけ `ai_summary` を代わりに使い、あとで入れ替える）。
- 返す件数・並び・ファイル名は今のまま。

## 検証

- 実装前に、lethe の context が効いているかを確認する: 12,000 字を超える実記事 1 件の末尾にだけある
  固有名詞を、要約に拾えるか（`ollama ps` の `CONTEXT` 列も見る）。拾えなければ入力上限を下げる。
- 実記事 5 件で参照用要約を作り、`【事実】` の固有名詞・数値が本文と一致するか目視で確かめる。
- テスト（LLM はモック）:
  - `clip_for_llm`: 上限以下は無加工、超過で先頭・末尾が残り、長さが上限+区切り以内。
  - 出力検証: 見出しが欠けたもの・短すぎるもの・長すぎるものを保存しない。
  - ループの選択: Saved のみ、`saved_at DESC`、NULL が先、版が古いものが後、バックオフ中は飛ばす。
  - basis: 本文あり→`content`、本文なし・抜粋あり→`excerpt`、両方空→対象外。
  - export: 5 欄が出る、未生成は `null`、既存欄が変わらない。
  - 既存のジャンル・タグ・`ai_summary` のテストが通る。

## リリース

2 つに分ける。

1. `fix/summary-input-truncation`（v0.14.5）: 設計 1。`clip_for_llm`、定数化、2 箇所の置き換え、テスト。
2. `feat/reference-summary`（v0.15.0）: 設計 2・3。列の追加（`ALTER TABLE`）、Phase 4、export、テスト、
   README・CLAUDE.md の更新。

どちらも AGENTS.md の作業フロー（ブランチ → テスト → PR → `--no-ff` マージ → バージョン更新 →
タグ → README → push → LaunchAgent の再読込）に従う。

## 決定事項

- export は認証なしで運用する。tailnet 内に閉じており、中身は記事情報のみのため（2026-11-22 ユーザー判断）。
  `feat/export-auth` は作らない。
- lethe への接続先は `http://100.123.63.64:11434/v1`（IP 直指定）で確定。Ollama は tailnet ホスト名
  （Host ヘッダー）を受けず FQDN だと 403 になるため、FQDN には戻さない。IP は tailnet の再採番で
  止まる（2026-09-08 の前例）ので、LLM 呼び出しが全滅したら lethe の現在 IP を確認する。

## 未決事項

- `question_suggester`（2,000 / 1,500 字）と `tagger`（500 字）の切り詰めは、今回は直さない。

## 実装者に任せること

- `clip_for_llm` の区切り文字列と配置（`summary_rules.py` か専用モジュールか）。
- 参照用要約のプロンプトの細かい文言。ただし 4 部構成・原文表記・出どころの区別は守る。
- Phase 4 の待機間隔（他のループと同じ 10 秒を既定にする）。
