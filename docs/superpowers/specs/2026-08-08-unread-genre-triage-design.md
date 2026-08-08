# 未読記事のジャンル別トリアージ — 設計

## 背景・目的

未読が溜まり続けて処理しきれない。実測（2026-08-08 時点）では未読 648 件のうち、はてなブックマーク総合 366 件・Yahoo!ニュース主要 120 件・Qiita 116 件の 3 フィードで 93% を占める。これらは雑多で、1 件ずつ開いて判断する運用では追いつかない。

一方で既存の Recommend / Unrecommend は「保存記事のタグとの重なり」による二分で、Unrecommend 287 件をまとめて処理する手段が無い。フィード単位の一括既読はあるが、はてブ総合のように 1 フィードの中身が雑多な場合は「このフィードを丸ごと捨てる」判断ができない。

そこで **未読記事をジャンル別に束ね、束ごとにまとめて読む / 捨てる** 導線を作る。「スポーツ 21 件は要らない」「事件・災害 56 件は要らない」という粒度で処理できるようにするのが目的。

## 設計上の前提（調査で確定した事実）

### 1. AI タグ候補をそのままジャンルには使えない

未読 644 件に付いた `tag_suggestions` は **447 種**、うち **287 種が 1 件のみ**。上位は `technology` 190 件・`news` 136 件と粗すぎる。さらに 1 記事に最大 3 タグ付くため束が重複所属になり、「この束を捨てる」が集合として成立しない。

### 2. LLM に語彙を守らせることはできない

`background_processor.py:119` は既に `Tag` テーブル 68 語を全件読んで `existing_tags` として渡し、プロンプトにも `"If existing tags are provided, reuse them when appropriate"` と書いている。それでも実測で **異なりタグの 89%・出現回数の 73% が語彙外**。`ai`(83)/`llm`(30)、`disaster`(35)/`accident`(12)/`earthquake`(10)/`weather`(5) のように同義語へ散る。

一括削除は「その束が該当記事を漏れなく含む」ことに依存する操作なので、この散らばりは致命的になる。よって **ジャンル分類は LLM ではなく決定的な辞書写像で行う**。LLM 由来の揺らぎを吸収する層としてジャンルを置く、という位置づけ。

（参考: LLM 分類を選んだ場合、既存 644 件の再生成に 1 コール実測 12.4 秒 × 644 ÷ bulk 2 ワーカー ≒ 67 分を要し、その間 Ollama が占有される。辞書なら再適用は秒で済み、誤りは辞書を直して再実行できる。）

### 3. 分類品質は辞書より「解決規則」で決まる

タグ複数ヒット時に「最も具体的（＝出現頻度が低い）タグを採る」規則は誤爆した（`['sound','emergency']` → エンタメ、`['okinawa','politics']` → 事件・災害）。**ジャンルに固定の優先順位を与えて解決する**規則に変えたところ、`['ai','programming']` → AI のように素直に決まり、未読 627 件が以下に分かれた。

```
dev 118 (19%)  ai 98 (16%)  life 63 (10%)  politics 61 (10%)  incident 56 (9%)
entertainment 55 (9%)  other 50 (8%)  science 45 (7%)  economy 40 (6%)
sports 21 (3%)  security 20 (3%)
```

`other` 8% の中身は `['sound','emergency']` `['working-holiday','journey']` など、実際に分類しづらいものだった。

### 4. ジャンルはフェッチ時点では確定しない

`tag_suggestions` は取得後に background processor が付ける。したがって「このジャンルは取り込まない」は原理的に作れない。流入抑制は本スペックの範囲外とし、本スペックが提供する分類と dismissed の上に「自動 dismiss ルール」として後続スペックで載せる。

## スコープ

**含む**: ジャンル分類器、編集可能なジャンル定義（DB + CRUD + 管理 UI）、ジャンル別の未読一覧、ジャンル単位の一括既読・一括 dismiss、Dismissed ビューと復元。

**含まない**: 流入抑制（自動 dismiss ルール）、dismissed 記事の自動削除、ジャンルの自動学習（どのタグをどのジャンルに置くかを利用実績から提案する類）。

## 「捨てる」の定義

`is_read` を立てる方式は採らない。`article_cleanup.py:30-34` の削除条件は `is_saved == False AND is_read == True AND 基準日時 < cutoff` の AND であり、既読化した瞬間に自動削除の保護が外れるため。実測で未読 637 件中 12 件は既に 60 日より古く、既読化すれば次のフェッチ直後に消える。

代わりに `dismissed_at` を新設する。一覧と未読カウントからは外れるが `is_read` は `False` のままなので、**dismissed 記事は自動削除の対象に入らない**（`article_cleanup.py` は変更不要）。誤って捨てた記事は Dismissed ビューまたは検索から復元できる。

dismissed 記事の保持期間は設けない。1 記事は数 KB であり、溜まって困る規模になってから別途検討する。

## データモデル

### ジャンル定義（編集可能にする）

ジャンルの粒度と分け方は使いながら直すものなので、コード内定数ではなく DB に置き、既存の `ExcludePattern`（`models.py:87`, `routers/exclude_patterns.py`）と同じ CRUD + サイドバー UI の作法で編集できるようにする。ルールを変えたらその場で既存記事へ適用される点も、除外パターンが追加時に既存記事を即 purge するのと揃える。

```python
class Genre(Base):
    __tablename__ = "genres"
    id: int
    key: str        # 一意。API とフィルタで使う英語キー（例: "ai"）
    label_ja: str   # 表示名（例: "AI・LLM"）
    priority: int   # 小さいほど優先。タグが複数ジャンルにヒットしたときの解決順
    created_at: str

class GenreRule(Base):
    __tablename__ = "genre_rules"
    id: int
    tag: str          # 一意。tag_suggestions に現れる英語タグ
    genre_id: int     # ForeignKey("genres.id", ondelete="CASCADE")
    is_generic: bool  # True のルールは他に手がかりが無いときだけ使う（technology, news 等）
```

どちらも新規テーブルなので `create_all` が作る（`ALTER TABLE` は不要）。

`other` は予約キーとし、DB には置かない。どのルールにも当たらなかった記事が入る受け皿であり、`genres` に行が無いため `GenreRule` から参照することもできない。

初回起動時、`genres` が空のときだけ後述の初期辞書をシードする（`main.py` の lifespan、既存のバックフィルと同じ位置）。シード値は `app/services/genre_seed.py` に定数として置く。以後シードは走らないので、ユーザーの編集が上書きされることはない。

### Article への追加カラム

`Article` に 2 カラムを追加する。`create_all` は既存テーブルを変更しないため、`main.py` の既存の手動マイグレーション（`main.py:75-89` の `PRAGMA table_info` → `ALTER TABLE` パターン）に追記する。

```python
if "genre" not in existing_article_cols:
    await conn.execute(text("ALTER TABLE articles ADD COLUMN genre TEXT"))
if "dismissed_at" not in existing_article_cols:
    await conn.execute(text("ALTER TABLE articles ADD COLUMN dismissed_at TEXT"))
await conn.execute(
    text("CREATE INDEX IF NOT EXISTS idx_articles_genre ON articles(genre)")
)
```

- `genre TEXT NULL` — 分類結果を保存する。一覧のグループ集計を SQL の `GROUP BY` で回すため、都度計算はしない。
- `dismissed_at TEXT NULL` — 捨てた日時（ISO8601 文字列。既存の日時カラムと同じ形式）。`NULL` でない行が dismissed。

`models.py` の `Article` にも同名の `Column` を追加する。

## ジャンル分類器

新規モジュール `backend/app/services/genre_classifier.py`。分類そのものは DB に触らない純関数とし、ルールは呼び出し側が渡す。こうすることで辞書が DB 由来になってもテストは固定値で書ける。

```python
@dataclass(frozen=True)
class GenreRules:
    """DB から組み立てた分類ルールのスナップショット。"""
    tag_to_genre: dict[str, str]      # 通常ルール: tag -> genre key
    generic_to_genre: dict[str, str]  # 汎用ルール: tag -> genre key
    priority: dict[str, int]          # genre key -> priority（小さいほど優先）

def classify(tags: list[str], rules: GenreRules) -> str:
    """タグ候補からジャンルを 1 つ決める。該当なしは "other"。"""

async def load_rules(session: AsyncSession) -> GenreRules:
    """genres / genre_rules から GenreRules を組み立てる。"""
```

解決規則:

1. `tags` を `tag_to_genre` で引き、ヒットしたジャンルのうち `priority` が最小のものを返す。`priority` が同値の場合は `key` の辞書順で決める（管理 UI は上下移動で一意な値を振るが、API を直接叩けば同値になりうるため、分類結果が実行ごとに揺れないようにする）。
2. ヒットが無ければ `generic_to_genre` を引き、**通常ルールと同じく `priority` 最小のものを返す**。`tags` の並び順では決めない（並び順は LLM 出力の順であり無秩序なため、汎用ルールが増えたときに分類結果が説明不能になる）。
3. それも無ければ `"other"`。

ルール表は 150 行程度と小さく、SQLite への問い合わせも安いので、キャッシュは持たず必要な箇所で都度 `load_rules()` する。一括再分類のように多数の記事を回す場合だけ、呼び出し側でループの外に 1 回出す。

初期辞書（シード値）は調査で検証済みの以下とする（優先順位順）。地名（`okinawa`, `kumamoto`）のような修飾語は誤爆源なので入れない。汎用ルールは `technology` → `dev` の 1 件のみをシードする（`news`/`japan`/`japanese` はどのジャンルにも寄せず、ルール無しのまま `other` に落とす。`other` は `genres` に行を持たないので `GenreRule.genre_id` から参照できない）。

| 優先 | ジャンル | 主なタグ |
|---|---|---|
| 1 | `ai` | ai, llm, openai, claude, rag, mcp, genai, chatgpt, gemini, nvidia |
| 2 | `security` | security, privacy, vulnerability, malware |
| 3 | `dev` | programming, web, javascript, python, rust, unity, database, api, github, linux, windows, microsoft, software, hardware, network, excel, performance, cloud, aws, vscode, it, tools, data |
| 4 | `sports` | baseball, sports, sport, soccer |
| 5 | `incident` | disaster, accident, earthquake, weather, crime, safety |
| 6 | `politics` | government, politics, policy, geopolitics, law, war, local-government, copyright, gender, labor, disability |
| 7 | `economy` | finance, economy, business, tax, yen, accounting, payment, marketing, retail, consumer, career, monetization |
| 8 | `science` | research, psychology, education, university, mathematics, medical, agriculture, wildlife, logic, infection, space, animal |
| 9 | `culture` | history, museum, architecture, art, literature, design, writing, media, culture |
| 10 | `entertainment` | entertainment, game, manga, anime, movie, music, comedy, story, science-fiction, comic, book |
| 11 | `life` | health, life, lifestyle, daily-life, food, recipe, travel, relationship, emotion, mental-health, home, weather, society, social, community, communication, social-media, railway, transportation |

`life` は「生活・健康」に絞り、歴史・建築・美術・出版などは `culture` に分ける。1 つのジャンルが広すぎると「まとめて捨てる」判断が効かなくなるため（表示名から想像する中身と実際の中身がずれる）。同じ理由で `weather` を `incident` から `life` へ移した。通常の天気記事まで「事件・災害」として一括処理されるのを避ける。

この調整後の実測分布（未読 617 件）:

```
dev 116 (19%)  ai 96 (16%)  politics 60 (10%)  incident 50 (8%)  other 50 (8%)
science 45 (7%)  life 42 (7%)  culture 41 (7%)  economy 39 (6%)
entertainment 37 (6%)  sports 21 (3%)  security 20 (3%)
```

日本語表示名は `genres.label_ja` に持つ（`ai` → 「AI・LLM」、`dev` → 「開発・技術」、`incident` → 「事件・災害」など）。フロントは API が返す表示名をそのまま使い、定数表を持たない。

辞書は完成品ではなく育てる前提とする。`other` の記事一覧をフロントから見られるようにしておき、目立つ語が溜まったらジャンル管理画面から割り当てる。

## 分類の適用タイミング

1. **新規・更新時** — `background_processor.py` で `tag_suggestions` を書く 2 箇所（`_process_phase1_one`、`_process_phase2_one`）で、同じトランザクション内で `article.genre = classify(tags, rules)` を設定する。
2. **バックフィル** — `main.py` の lifespan、既存の `normalized_url` バックフィルと同じ位置で、`genre IS NULL AND tag_suggestions IS NOT NULL` の行を分類して埋める。**シード投入を必ずバックフィルより先に実行する。** 順序が逆だと空のルールで全件が `genre="other"` に確定し、`genre IS NULL` を条件とする以後のバックフィルでは二度と拾えなくなる（再分類 API を叩くまで誰も気づかない）。
3. **ルール変更時の再分類** — ジャンル / ルールを追加・変更・削除する API はいずれも、コミット後にその場で全件再分類を実行し、レスポンスに `reclassified` 件数を含める（`POST /exclude-patterns` が追加時に既存記事を purge して `purged` を返すのと同じ作法）。LLM を呼ばず `tag_suggestions` を読み直すだけなので、数千件でも一瞬で終わる。再分類だけを単独で実行できるよう `POST /articles/reclassify-genres` も用意する。

`tag_suggestions` が無い記事の `genre` は `NULL` のままとし、ジャンル一覧では「未分類」として扱わない（背景処理が進めば自然に埋まるため）。

## API

ジャンル定義の CRUD は新規ルータ `app/routers/genres.py`（`exclude_patterns.py` と同じ構成）に、記事側の操作は `app/routers/articles.py` に置く。

### ジャンル定義の編集

- `GET /genres` → `[{"id":1,"key":"ai","label_ja":"AI・LLM","priority":1,"rules":["ai","llm",...],"generic_rules":[]}]`
  ルールを同梱して返す。管理画面が 1 リクエストで描けるようにするため。
- `POST /genres` — body `{"key","label_ja","priority"}`。`key` 重複は 409、`key` が `"other"` の場合は 400（予約キー）。
- `PATCH /genres/{id}` — `label_ja` / `priority` を変更。`key` は変更不可（`Article.genre` と `ArticleFilters` が参照しているため。名前を変えたい場合は作り直す）。
- `DELETE /genres/{id}` — 紐づく `GenreRule` は DB のカスケードで消える。そのジャンルだった記事は再分類で他ジャンルか `other` に移る。
- `POST /genre-rules` — body `{"tag","genre_id","is_generic"}`。`tag` は小文字化して保存。既に他ジャンルに割り当て済みの `tag` は 409 ではなく **付け替え**（管理画面での移動が自然に行えるため）。
- `DELETE /genre-rules/{id}`

上記の変更系はすべてレスポンスに `reclassified` 件数を含める。

### 一覧・集計

- `GET /articles/genres` → `[{"genre": "ai", "label_ja": "AI・LLM", "unread_count": 98}, ...]`
  `is_read == False AND is_saved == False AND dismissed_at IS NULL AND genre IS NOT NULL` を `GROUP BY genre`、件数降順。表示名は `genres` テーブルから引く（`other` は表示名「その他」を固定で返す）。
- `GET /articles` に `genre: str | None` クエリを追加。既存の `feed_id` などと同じく `stmt`/`count_stmt` 双方に `where` を足す。
- `GET /articles` に `dismissed: bool = False` クエリを追加。`False` の既定時は `dismissed_at IS NULL` で絞り、`True` のときは `dismissed_at IS NOT NULL` のみを返す（Dismissed ビュー、`dismissed_at` 降順）。

### 一括操作

- `POST /articles/dismiss` — body `{"genre": "sports"}` または `{"ids": [1,2,3]}`。`genre` 指定・`ids` 指定のどちらでも対象は `is_saved == False AND dismissed_at IS NULL` の記事に限る（保存済みは常に保護。`ids` に保存済みが含まれていても無視して件数に数えない）。**`genre` 指定時はさらに `is_read == False` も条件に加える**（UI の確認ダイアログは `GET /articles/genres` の `unread_count` を見せているため、実処理も未読限定にしないと確認件数と実処理件数がずれる）。`ids` 指定は明示的な選択なので `is_read` は問わない。`dismissed_at` に現在時刻を入れ、`{"dismissed": <件数>}` を返す。
- `POST /articles/undismiss` — 同じ body 形式。対象は `dismissed_at IS NOT NULL` の記事。`dismissed_at` を `NULL` に戻し、`{"restored": <件数>}` を返す。
- 一括既読は既存の `POST /articles/mark-all-read` に `genre` パラメータを追加して賄う（新規エンドポイントを作らない）。`genre` 指定時の対象からは dismissed 記事を除き、**`is_saved == False` も条件に加える**。既存の `mark_all_read`（`articles.py:513`）は `is_read == False` だけで絞っており保存済み未読も既読化してしまうが、一括 dismiss が保存済みを保護する以上、genre 一括だけ保護しないのは非対称で事故のもとになる。全体・フィード指定時の既存挙動も `dismissed_at IS NULL` を条件に加える（非表示記事を開くだけで既読化され自動削除の保護が外れる事故を防ぐため。既読化そのものの挙動は変えない）。

`genre` と `ids` の両方が空の body は 422 を返す。両方指定された場合は `ids` を優先する。

### dismissed の除外範囲

原則: **通常の読書導線からは外し、管理・復旧導線には出す。**

| 対象 | 挙動 |
|---|---|
| `GET /articles`（既定） | 除外 |
| `GET /articles/recommended` | 除外 |
| `GET /articles/unrecommended` | 除外 |
| `GET /feeds` の `unread_count`（`feeds.py:20`） | 除外 |
| `GET /articles/search` | **含める**（誤って捨てた記事の復旧経路として必要） |
| `GET /articles/extract-failed` | **含める**（本文抽出の管理キュー。隠すと再試行も削除もできないゴミが残る） |
| `GET /articles/{id}` | 含める（直接参照は常に可能） |

`GET /articles/recommended` と `GET /articles/unrecommended` は `list_articles` とは別関数で独自に WHERE を組んでいる（`articles.py:90` / `articles.py:176`）。`GET /articles` に条件を足しただけでは反映されないので、**それぞれの関数に個別に `dismissed_at IS NULL` を追加する**。

検索結果と抽出失敗一覧に dismissed が混ざるため、`ArticleOut` に `dismissed_at` を追加してフロントでバッジ表示できるようにする。

## 既存機能への影響

### 重複記事の統合（必須の修正）

`deduplicator.py` の `_merge_into_keeper()` は loser から keeper へ引き継ぐフィールドを明示列挙している（`is_read` / `is_saved` を個別処理し、`("content", "ai_summary", "tag_suggestions", "image_url")` をループでコピー）。**ここに `dismissed_at` と `genre` を足さないと、dedup のたびに dismissed 状態が消える。**

dedup は `fetch_all_feeds()` から毎フェッチ後に自動実行され、生存優先順位は「保存済み > 非はてなブックマーク由来 > 古い `fetched_at` > 小さい id」。捨てた記事の多くははてブ総合経由（未読の 57%）なので、同じ URL の記事が元サイトのフィードから後に来ると keeper が非はてブ側になり、**捨てたはずの記事が未読として復活する**。

修正内容:

- `dismissed_at` — `is_read` と同じく OR 的に伝播する。`loser.dismissed_at` があり `keeper.dismissed_at` が `NULL` なら keeper に入れる。片方でも捨てられていれば捨てられたままにする。
- `genre` — `("content", "ai_summary", ...)` のループに足す（keeper 側が `NULL` のときだけコピー）。keeper の `tag_suggestions` が loser 由来に差し替わる場合もあるため、マージ後に `keeper.genre = classify(...)` で計算し直す方が正確。実装はこちらを採る。

## フロントエンド

- `types.ts` の `ArticleFilters` に `genre?: string`、`dismissed?: boolean` を追加。`Article` に `dismissed_at: string | null`。
- `hooks/useArticles.ts` に `useGenreCounts()`（`GET /articles/genres`、クエリキー `['genre-counts']`）と `useDismiss()` / `useUndismiss()` を追加。ジャンル定義 CRUD 側のキーは `['genres']` とし、名前で取り違えないようにする。
- ミューテーション成功時の invalidate は `['articles']` `['genre-counts']` `['feeds']` `['recommended-count']` `['unrecommended-count']`。既存の `useMarkAllRead`（`useArticles.ts:54`）が invalidate している対象に揃える。dismissed は Recommend / Unrecommend からも除外されるので、これを落とすとサイドバーのバッジが古いまま残る。一括操作は影響範囲が広いため、既存の in-place マージ方針は適用しない。
- `FeedSidebar.tsx` に「ジャンル」セクションを追加。フィード一覧と同じ見た目で、ジャンル名（`label_ja`）と未読件数バッジを並べる。件数 0 のジャンルは表示しない。最下部に Dismissed ビューへの導線を置く。
- 同じく `FeedSidebar.tsx` に「ジャンル管理」を追加する。既存の `除外パターン管理` と同じ位置・同じモーダルの作法で、次を行えるようにする。
  - ジャンルの追加・削除、表示名の変更、優先順位の変更（上下移動ボタンで `priority` を入れ替える）
  - ジャンルごとのタグをチップで一覧し、削除できる
  - タグを追加する入力欄。ジャンルを選んで `tag` を入れる。他ジャンルに割り当て済みのタグはその場で付け替わる
  - 汎用ルール（`is_generic`）はチェックボックスで区別し、チップの見た目も変える
  - 変更のたびにレスポンスの `reclassified` 件数をトーストなしの短いテキストで表示し、`['articles']` `['article-genres']` `['genres']` を invalidate する
  - `other` に落ちている記事へは、この画面からワンクリックで一覧（`?genre=other`）へ飛べるようにする。辞書を育てる導線がここで閉じる
- `ArticleList.tsx` のツールバーに、`filters.genre` が設定されているときだけ「まとめて既読」「まとめて非表示」を出す。既存の `重複記事を整理` などと同様に、件数を含む `confirm()` を挟む。
- **検索中（`searchQuery` が非空）は一括ボタンを無効化する。** `useSearchArticles`（`useArticles.ts:143`）は `{ feed_id, is_saved }` しか受け取らず `genre` を渡せない独立モードであり、検索で絞り込んだ表示に対して一括操作を押しても API には `{"genre": ...}` だけが飛び、**画面に見えている数件ではなくそのジャンルの全未読が処理される**。無効化時はツールチップで理由を示す。
- 実行直後にツールバー下へ `56 件を非表示にしました　[元に戻す]` を出し、その場で `undismiss` を呼べるようにする（対象 ID を控えておく）。次の操作か画面遷移で消える。一括操作は多数の記事の扱いを一度に決めるため、事前の `confirm()` だけでなく事後の撤回経路を持たせる。
- Dismissed ビューでは同じ位置に「まとめて戻す」を出す。
- 検索結果・抽出失敗一覧のカードで `dismissed_at` が非 `NULL` の記事に「非表示」バッジを出す（`ExtractStatusBadge` と同じ作りの小さなバッジ）。

UI 文言は「捨てる」ではなく「非表示にする」で統一する。この設計の核心は「既読化せず自動削除から守り、後で戻せる」という可逆性なのに、「捨てる」は消えて戻らない印象を与え、実際の挙動を利用者に伝えないため。内部名は `dismissed` のままとする。

## テスト

`backend/tests/test_genre_classifier.py`（新規、`GenreRules` を固定値で組んで純関数を検証）:

- 単一タグがルールに一致する場合、そのジャンルを返す
- 複数ジャンルにヒットする場合、`priority` が小さい方を返す（`['ai','programming']` → `ai`、`['game','soccer']` → `sports`）
- 通常ルールに無く汎用ルールにある場合、汎用を経由する（`['technology']` → `dev`）
- 通常ルールがあれば汎用ルールより優先される（`['technology','baseball']` → `sports`）
- どこにも該当しない場合 `other` を返す（`['working-holiday','journey']`）
- 空リストで `other` を返す

`backend/tests/test_genres_api.py`（新規）:

- シード後に `GET /genres` が初期辞書を返す
- `POST /genre-rules` で既に他ジャンルにあるタグを送ると付け替わり、二重登録されない
- ジャンル削除でそのジャンルのルールも消え、該当記事が再分類される
- 変更系のレスポンスに `reclassified` 件数が入る
- `key="other"` のジャンル作成が 400、`key` 重複が 409

`backend/tests/test_dismiss.py`（新規）:

- `POST /articles/dismiss` に `genre` を渡すと、そのジャンルの未読記事だけが dismissed になる
- 保存済み記事は `genre` 一括の対象外
- dismissed 記事が `GET /articles` の既定に出てこない
- dismissed 記事が `GET /articles/search` には出る
- `POST /articles/undismiss` で元に戻り、再び `GET /articles` に現れる
- `genre` も `ids` も無い body で 422
- dismissed 記事が `GET /articles/recommended` と `GET /articles/unrecommended` にも出てこない
- `POST /articles/mark-all-read` に `genre` を渡したとき、保存済み未読記事が既読にならない

`backend/tests/test_deduplicator.py`（既存ファイルに追記）:

- dismissed の loser と未 dismiss の keeper をマージすると、keeper が dismissed のまま残る（捨てた記事が dedup で復活しない）
- マージ後に keeper の `genre` が再計算される

フロントエンドはテスト基盤が無いため、実データでの目視確認とする。

## 段階リリース

分類を先に投入して品質を見てから操作系を足す。

1. `genres` / `genre_rules` テーブル + シード + 分類器 + `Article` のカラム 2 本（`genre` と `dismissed_at` はまとめて足す。使わないカラムがあっても無害）+ バックフィル + `GET /articles/genres`。分類結果は API のレスポンスで確認する。
2. ジャンルフィルタ + サイドバーのジャンルセクション（読むだけ）。ここで実データの分類品質を目で見る。
3. ジャンル定義の CRUD API + ジャンル管理 UI。辞書を直せるようにする。
4. 一括操作 + Dismissed ビュー + 各一覧からの dismissed 除外 + `_merge_into_keeper` の引き継ぎ修正。dedup 修正は dismissed を導入する第 4 段階と同時に入れる（先に入れても意味が無く、後に回すと捨てた記事が復活する窓ができる）。

第 3 段階を第 4 段階より前に置くのは、**捨てる操作を入れる前に辞書を直せる状態にしておく**ため。分類が粗いまま一括 dismiss を使えるようにすると、読むべき記事をまとめて捨てる事故が起きやすい。

各段階は単体でデプロイ可能で、前段が壊れていないことを確認してから次へ進む。
