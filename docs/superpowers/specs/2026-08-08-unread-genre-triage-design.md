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

**含む**: ジャンル分類器、ジャンル別の未読一覧、ジャンル単位の一括既読・一括 dismiss、Dismissed ビューと復元。

**含まない**: 流入抑制（自動 dismiss ルール）、ジャンルの UI 上での編集、dismissed 記事の自動削除。

## 「捨てる」の定義

`is_read` を立てる方式は採らない。`article_cleanup.py:30-34` の削除条件は `is_saved == False AND is_read == True AND 基準日時 < cutoff` の AND であり、既読化した瞬間に自動削除の保護が外れるため。実測で未読 637 件中 12 件は既に 60 日より古く、既読化すれば次のフェッチ直後に消える。

代わりに `dismissed_at` を新設する。一覧と未読カウントからは外れるが `is_read` は `False` のままなので、**dismissed 記事は自動削除の対象に入らない**（`article_cleanup.py` は変更不要）。誤って捨てた記事は Dismissed ビューまたは検索から復元できる。

dismissed 記事の保持期間は設けない。1 記事は数 KB であり、溜まって困る規模になってから別途検討する。

## データモデル

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

新規モジュール `backend/app/services/genre_classifier.py`。

```python
GENRE_PRIORITY: list[tuple[str, list[str]]]  # 優先順位順。先に来たジャンルが勝つ
GENERIC_FALLBACK: dict[str, str]             # 汎用タグ → ジャンル

def classify(tags: list[str]) -> str:
    """タグ候補からジャンルを 1 つ決める。該当なしは "other"。"""
```

解決規則:

1. `tags` を `GENRE_PRIORITY` の辞書で引き、ヒットしたジャンルのうち **優先順位が最も高いもの** を返す。
2. ヒットが無ければ `GENERIC_FALLBACK`（`technology` → `dev`、`news`/`japan`/`japanese` → `other`）を順に引く。`data` は `dev` の通常辞書に入れるので、フォールバックには重複して置かない。
3. それも無ければ `"other"`。

初期辞書は調査で検証済みの以下とする（優先順位順）。地名（`okinawa`, `kumamoto`）のような修飾語は誤爆源なので辞書に入れない。

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
| 9 | `entertainment` | entertainment, game, manga, anime, movie, music, comedy, art, story, literature, book, science-fiction, comic |
| 10 | `life` | health, life, lifestyle, daily-life, food, recipe, travel, relationship, emotion, mental-health, home, history, culture, society, social, community, communication, social-media, railway, transportation, architecture, museum, design, writing, media |

日本語表示名はフロントエンド側の定数表で持つ（バックエンドは英語キーのみを返す）。

辞書は完成品ではなく育てる前提とする。`other` の記事一覧をフロントから見られるようにしておき、目立つ語が溜まったら辞書に足して再分類する。

## 分類の適用タイミング

1. **新規・更新時** — `background_processor.py` で `tag_suggestions` を書く 2 箇所（Phase 1 の `_process_one`、Phase 2 のタグバックフィル）で、同じトランザクション内で `article.genre = classify(tags)` を設定する。
2. **バックフィル** — `main.py` の lifespan、既存の `normalized_url` バックフィルと同じ位置で、`genre IS NULL AND tag_suggestions IS NOT NULL` の行を分類して埋める。
3. **辞書更新後の再分類** — 管理エンドポイント `POST /articles/reclassify-genres` で全件再分類する（既存の `regenerate-summaries` と同じ管理系の位置づけ）。LLM を呼ばないので即座に完了する。

`tag_suggestions` が無い記事の `genre` は `NULL` のままとし、ジャンル一覧では「未分類」として扱わない（背景処理が進めば自然に埋まるため）。

## API

すべて `app/routers/articles.py` に追加する。

### 一覧・集計

- `GET /articles/genres` → `[{"genre": "ai", "unread_count": 98}, ...]`
  `is_read == False AND is_saved == False AND dismissed_at IS NULL AND genre IS NOT NULL` を `GROUP BY genre`、件数降順。
- `GET /articles` に `genre: str | None` クエリを追加。既存の `feed_id` などと同じく `stmt`/`count_stmt` 双方に `where` を足す。
- `GET /articles` に `dismissed: bool = False` クエリを追加。`False` の既定時は `dismissed_at IS NULL` で絞り、`True` のときは `dismissed_at IS NOT NULL` のみを返す（Dismissed ビュー、`dismissed_at` 降順）。

### 一括操作

- `POST /articles/dismiss` — body `{"genre": "sports"}` または `{"ids": [1,2,3]}`。`genre` 指定・`ids` 指定のどちらでも対象は `is_saved == False AND dismissed_at IS NULL` の記事に限る（保存済みは常に保護。`ids` に保存済みが含まれていても無視して件数に数えない）。`dismissed_at` に現在時刻を入れ、`{"dismissed": <件数>}` を返す。
- `POST /articles/undismiss` — 同じ body 形式。対象は `dismissed_at IS NOT NULL` の記事。`dismissed_at` を `NULL` に戻し、`{"restored": <件数>}` を返す。
- 一括既読は既存の `POST /articles/mark-all-read` に `genre` パラメータを追加して賄う（新規エンドポイントを作らない）。`genre` 指定時の対象からは dismissed 記事を除く。

`genre` と `ids` の両方が空の body は 422 を返す。両方指定された場合は `ids` を優先する。

### dismissed の除外範囲

| 対象 | 挙動 |
|---|---|
| `GET /articles`（既定） | 除外 |
| `GET /articles/recommended` / `unrecommended` | 除外 |
| `GET /feeds` の `unread_count`（`feeds.py:20`） | 除外 |
| `GET /articles/search` | **含める**（誤って捨てた記事の復旧経路として必要） |
| `GET /articles/{id}` | 含める（直接参照は常に可能） |

検索結果に dismissed が混ざるため、`ArticleOut` に `dismissed_at` を追加してフロントでバッジ表示できるようにする。

## フロントエンド

- `types.ts` の `ArticleFilters` に `genre?: string`、`dismissed?: boolean` を追加。`Article` に `dismissed_at: string | null`。
- `hooks/useArticles.ts` に `useGenres()`（`GET /articles/genres`）と `useDismiss()` / `useUndismiss()` を追加。ミューテーション成功時は `['articles']` `['article-genres']` `['feeds']` を invalidate する（一括操作は影響範囲が広く、既存の in-place マージ方針は適用しない）。
- `FeedSidebar.tsx` に「ジャンル」セクションを追加。フィード一覧と同じ見た目で、ジャンル名（日本語表示名）と未読件数バッジを並べる。件数 0 のジャンルは表示しない。最下部に Dismissed ビューへの導線を置く。
- `ArticleList.tsx` のツールバーに、`filters.genre` が設定されているときだけ「まとめて既読」「まとめて捨てる」を出す。既存の `重複記事を整理` などと同様に、件数を含む `confirm()` を挟む。
- Dismissed ビューでは同じ位置に「まとめて戻す」を出す。
- 検索結果とリストのカードで `dismissed_at` が非 `NULL` の記事に「捨てた」バッジを出す（`ExtractStatusBadge` と同じ作りの小さなバッジ）。

## テスト

`backend/tests/test_genre_classifier.py`（新規）:

- 単一タグが辞書に一致する場合、そのジャンルを返す
- 複数ジャンルにヒットする場合、優先順位が高い方を返す（`['ai','programming']` → `ai`、`['game','soccer']` → `sports`）
- 辞書に無いタグのみの場合、`GENERIC_FALLBACK` を経由する（`['technology']` → `dev`）
- どこにも該当しない場合 `other` を返す（`['working-holiday','journey']`）
- 空リストで `other` を返す

`backend/tests/test_dismiss.py`（新規）:

- `POST /articles/dismiss` に `genre` を渡すと、そのジャンルの未読記事だけが dismissed になる
- 保存済み記事は `genre` 一括の対象外
- dismissed 記事が `GET /articles` の既定に出てこない
- dismissed 記事が `GET /articles/search` には出る
- `POST /articles/undismiss` で元に戻り、再び `GET /articles` に現れる
- `genre` も `ids` も無い body で 422

フロントエンドはテスト基盤が無いため、実データでの目視確認とする。

## 段階リリース

分類を先に投入して品質を見てから操作系を足す。

1. 分類器 + カラム 2 本の追加（`genre` と `dismissed_at` はまとめて足す。使わないカラムがあっても無害）+ バックフィル + `GET /articles/genres`。API のレスポンスで分類結果を確認し、必要なら辞書を直して `POST /articles/reclassify-genres` で再適用する。
2. ジャンルフィルタ + サイドバーのジャンルセクション（読むだけ）。
3. 一括操作 + Dismissed ビュー + 各一覧からの dismissed 除外。

各段階は単体でデプロイ可能で、前段が壊れていないことを確認してから次へ進む。
