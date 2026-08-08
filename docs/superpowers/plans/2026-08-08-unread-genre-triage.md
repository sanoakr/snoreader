# 未読記事のジャンル別トリアージ 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 未読記事を編集可能な辞書でジャンル分類し、ジャンル単位でまとめて既読化・非表示化できるようにする。

**Architecture:** タグ候補（`tag_suggestions`）→ ジャンルの写像を DB（`genres` / `genre_rules`）に持ち、決定的な純関数で 1 記事 1 ジャンルに解決する。「非表示」は `is_read` を立てずに `dismissed_at` を入れる新状態とし、既存の自動削除（`is_saved=False AND is_read=True` が条件）から保護する。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.0 (async) / SQLite / pytest + pytest-asyncio / React 19 + TypeScript + TanStack Query + Tailwind v4

**設計スペック:** `docs/superpowers/specs/2026-08-08-unread-genre-triage-design.md`

## Global Constraints

- バックエンドの依存追加は `uv add`（`pip` は使わない）。今回は新規依存なし。
- コメントは日本語、識別子は英語。マジックナンバーは定数化する。
- ルータから `app.services.*` / `app.ai.*` を呼ぶときは関数本体内で遅延 import する（既存規約。`app.models` / `app.database` / `app.schemas` はトップレベル import で良い）。
- テストは `cd backend && uv run pytest`。API テストは `backend/tests/test_exclude_patterns.py` の `client` フィクスチャ方式（`tmp_path` に SQLite を作り `main_module.lifespan` を通す）を踏襲する。
- SQLAlchemy の `create_all` は既存テーブルを変更しない。既存テーブルへのカラム追加は `main.py` の `PRAGMA table_info` → `ALTER TABLE` パターンに追記する。新規テーブルは `create_all` が作るので追記不要。
- UI 文言は「捨てる」ではなく **「非表示」** で統一する。内部名は `dismissed` のまま。
- 予約キー `other` は `genres` テーブルに行を持たない。表示名は「その他」を固定で返す。
- フロントの `npm run build` が型チェックを兼ねる。`npm run lint` は本作業前から 19 errors 出ているため、**エラー件数を増やさないこと**を基準にする（`pinnedArticleRef` 由来の既存 `react-hooks/refs` 違反）。
- コミットは Conventional Commits、要約 50 字以内、英語。

---

## File Structure

**新規（バックエンド）**

- `backend/app/services/genre_classifier.py` — `GenreRules` データクラス、純関数 `classify()`、DB からルールを読む `load_rules()`、全件再分類 `reclassify_all()`
- `backend/app/services/genre_seed.py` — 初期辞書の定数と `seed_genres()`
- `backend/app/routers/genres.py` — ジャンル定義の CRUD
- `backend/tests/test_genre_classifier.py` — 純関数のテスト（DB 不要）
- `backend/tests/test_genres_api.py` — CRUD と再分類のテスト
- `backend/tests/test_dismiss.py` — 非表示 API と各一覧からの除外のテスト

**変更（バックエンド）**

- `backend/app/models.py` — `Genre` / `GenreRule` モデル追加、`Article` に `genre` / `dismissed_at`
- `backend/app/schemas.py` — `ArticleOut` に `dismissed_at`、ジャンル系スキーマ追加
- `backend/app/main.py` — ALTER TABLE 追記、シード → バックフィルの順で lifespan に組み込み、`genres` ルータ登録
- `backend/app/routers/articles.py` — `GET /articles/genres`、`genre` / `dismissed` フィルタ、dismiss / undismiss、`mark-all-read` の `genre`、recommended / unrecommended の除外
- `backend/app/services/background_processor.py` — `tag_suggestions` 書き込み時に `genre` も設定
- `backend/app/services/deduplicator.py` — `_merge_into_keeper` で `dismissed_at` を伝播し `genre` を再計算
- `backend/tests/test_deduplicator.py` — 上記の回帰テストを追記

**変更（フロントエンド）**

- `frontend/src/types.ts` — `ArticleFilters` に `genre` / `dismissed`、`Article` に `dismissed_at`、`GenreCount` / `GenreDef` 型
- `frontend/src/api/client.ts` — 追加エンドポイントの関数
- `frontend/src/hooks/useArticles.ts` — `useGenreCounts` / `useDismiss` / `useUndismiss`
- `frontend/src/hooks/useGenres.ts`（新規）— ジャンル定義 CRUD のフック
- `frontend/src/components/layout/FeedSidebar.tsx` — ジャンルセクション、Dismissed 導線、ジャンル管理モーダル
- `frontend/src/components/articles/ArticleList.tsx` — 一括操作ボタン、Undo 表示、「非表示」バッジ

---

### Task 1: ジャンル分類の純関数

**Files:**
- Create: `backend/app/services/genre_classifier.py`
- Test: `backend/tests/test_genre_classifier.py`

**Interfaces:**
- Consumes: なし
- Produces: `GenreRules(tag_to_genre: dict[str, str], generic_to_genre: dict[str, str], priority: dict[str, int])`、`classify(tags: list[str], rules: GenreRules) -> str`、定数 `OTHER_GENRE = "other"`

- [ ] **Step 1: 失敗するテストを書く**

`backend/tests/test_genre_classifier.py` を新規作成する。

```python
"""タグ候補 → ジャンルの決定的な写像のテスト。

分類は DB に触れない純関数なので、ルールを固定値で組んで検証する。
"""

from __future__ import annotations

import pytest

from app.services.genre_classifier import GenreRules, classify


@pytest.fixture
def rules() -> GenreRules:
    return GenreRules(
        tag_to_genre={
            "ai": "ai", "llm": "ai",
            "programming": "dev", "python": "dev",
            "baseball": "sports", "soccer": "sports",
        },
        generic_to_genre={"technology": "dev"},
        priority={"ai": 1, "dev": 3, "sports": 4},
    )


def test_single_tag_maps_to_its_genre(rules: GenreRules):
    assert classify(["python"], rules) == "dev"


def test_multiple_hits_resolve_by_priority(rules: GenreRules):
    """ai(1) と dev(3) に当たる場合、優先順位の小さい ai を採る。"""
    assert classify(["ai", "programming"], rules) == "ai"
    assert classify(["programming", "ai"], rules) == "ai"  # タグの並び順に依存しない


def test_generic_rule_used_when_no_normal_hit(rules: GenreRules):
    assert classify(["technology"], rules) == "dev"


def test_normal_rule_beats_generic_rule(rules: GenreRules):
    """通常ルールが 1 つでもあれば汎用ルールは見ない。"""
    assert classify(["technology", "baseball"], rules) == "sports"


def test_unknown_tags_fall_back_to_other(rules: GenreRules):
    assert classify(["working-holiday", "journey"], rules) == "other"


def test_empty_tags_return_other(rules: GenreRules):
    assert classify([], rules) == "other"


def test_priority_tie_broken_by_key_order():
    """priority が同値でも結果が揺れないこと。"""
    tied = GenreRules(
        tag_to_genre={"alpha": "zeta", "beta": "alpha"},
        generic_to_genre={},
        priority={"zeta": 5, "alpha": 5},
    )
    assert classify(["alpha", "beta"], tied) == "alpha"
    assert classify(["beta", "alpha"], tied) == "alpha"


def test_generic_rules_also_resolve_by_priority():
    """汎用ルールが複数当たる場合も並び順ではなく priority で決める。"""
    multi = GenreRules(
        tag_to_genre={},
        generic_to_genre={"news": "life", "technology": "dev"},
        priority={"dev": 3, "life": 11},
    )
    assert classify(["news", "technology"], multi) == "dev"
    assert classify(["technology", "news"], multi) == "dev"
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `cd backend && uv run pytest tests/test_genre_classifier.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.genre_classifier'`

- [ ] **Step 3: 最小の実装を書く**

`backend/app/services/genre_classifier.py` を新規作成する。

```python
"""タグ候補から記事のジャンルを 1 つ決める分類器。

LLM は記事をまたいだ語彙の一貫性を保てない（実測で語彙外タグが出現回数の 73%）。
一括操作は「その束が該当記事を漏れなく含む」ことに依存するため、分類は
編集可能な辞書による決定的な写像で行う。分類そのものは DB に触らない純関数とし、
ルールは呼び出し側がスナップショットとして渡す。
"""

from __future__ import annotations

from dataclasses import dataclass

# どのルールにも当たらなかった記事の受け皿。genres テーブルに行は持たない予約キー
OTHER_GENRE = "other"


@dataclass(frozen=True)
class GenreRules:
    """DB から組み立てた分類ルールのスナップショット。"""

    tag_to_genre: dict[str, str]      # 通常ルール: tag -> genre key
    generic_to_genre: dict[str, str]  # 汎用ルール: tag -> genre key
    priority: dict[str, int]          # genre key -> priority（小さいほど優先）


def _resolve(genres: list[str], rules: GenreRules) -> str:
    """候補ジャンルから priority 最小のものを返す。同値は key の辞書順で決める。"""
    return min(genres, key=lambda g: (rules.priority.get(g, 1_000_000), g))


def classify(tags: list[str], rules: GenreRules) -> str:
    """タグ候補からジャンルを 1 つ決める。該当なしは "other"。"""
    hits = [rules.tag_to_genre[t] for t in tags if t in rules.tag_to_genre]
    if hits:
        return _resolve(hits, rules)

    generic_hits = [rules.generic_to_genre[t] for t in tags if t in rules.generic_to_genre]
    if generic_hits:
        return _resolve(generic_hits, rules)

    return OTHER_GENRE
```

- [ ] **Step 4: テストを実行して通ることを確認**

Run: `cd backend && uv run pytest tests/test_genre_classifier.py -v`
Expected: PASS（8 tests）

- [ ] **Step 5: コミット**

```bash
git add backend/app/services/genre_classifier.py backend/tests/test_genre_classifier.py
git commit -m "feat: add deterministic genre classifier"
```

---

### Task 2: ジャンル定義テーブルとシード

**Files:**
- Create: `backend/app/services/genre_seed.py`
- Modify: `backend/app/models.py`（`Article` に 2 カラム、`Genre` / `GenreRule` を追加）
- Modify: `backend/app/main.py:75-93`（ALTER TABLE 追記）、lifespan のバックフィル位置
- Test: `backend/tests/test_genres_api.py`（このタスクではシードの検証まで）

**Interfaces:**
- Consumes: `GenreRules`, `OTHER_GENRE`（Task 1）
- Produces: モデル `Genre(id, key, label_ja, priority, created_at)` / `GenreRule(id, tag, genre_id, is_generic)`、`Article.genre` / `Article.dismissed_at`、`seed_genres(session) -> int`、定数 `GENRE_SEED: list[tuple[str, str, int, list[str]]]`（key, label_ja, priority, tags）、`GENERIC_SEED: dict[str, str]`

- [ ] **Step 1: 失敗するテストを書く**

`backend/tests/test_genres_api.py` を新規作成する。`client` フィクスチャは `tests/test_exclude_patterns.py` と同一の方式。

```python
"""ジャンル定義の CRUD と再分類のテスト。"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SNOREADER_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    from app import config as config_module

    config_module.settings = config_module.Settings()  # type: ignore[assignment]

    from app import database as database_module

    importlib.reload(database_module)

    from app import main as main_module

    importlib.reload(main_module)

    async with main_module.lifespan(main_module.app):
        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.mark.asyncio
async def test_seed_creates_initial_genres(client: AsyncClient) -> None:
    """起動時のシードで初期ジャンルとルールが入ること。API はまだ無いので DB を直接見る。"""
    from app.database import async_session
    from app.models import Genre, GenreRule
    from sqlalchemy import select

    assert client is not None  # lifespan を通すためにフィクスチャを使う

    async with async_session() as session:
        genres = (await session.execute(select(Genre).order_by(Genre.priority))).scalars().all()
        keys = [g.key for g in genres]
        assert keys[0] == "ai"
        assert "dev" in keys
        assert "other" not in keys  # 予約キーは DB に持たない
        assert all(g.label_ja for g in genres)

        ai_id = next(g.id for g in genres if g.key == "ai")
        ai_tags = (
            await session.execute(select(GenreRule.tag).where(GenreRule.genre_id == ai_id))
        ).scalars().all()
        assert "llm" in ai_tags

        generic = (
            await session.execute(
                select(GenreRule.tag).where(GenreRule.is_generic == True)  # noqa: E712
            )
        ).scalars().all()
        assert generic == ["technology"]


@pytest.mark.asyncio
async def test_seed_runs_only_once(client: AsyncClient) -> None:
    """2 回目の呼び出しでシードが重複投入されないこと。"""
    from app.database import async_session
    from app.models import Genre
    from app.services.genre_seed import seed_genres
    from sqlalchemy import func, select

    assert client is not None

    async with async_session() as session:
        before = await session.scalar(select(func.count()).select_from(Genre))
        assert await seed_genres(session) == 0
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(Genre)) == before
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `cd backend && uv run pytest tests/test_genres_api.py -v`
Expected: FAIL — `/api/genres` が 404（ルータ未実装）

- [ ] **Step 3: モデルを追加**

`backend/app/models.py` の `Article` に 2 カラムを足す。`extract_attempts` の直後に置く。

```python
    # タグ候補から決めたジャンル（genre_classifier）。null = 未分類
    genre: Mapped[str | None] = mapped_column(String, nullable=True)
    # 一覧から外した日時。is_read は変えないので article_cleanup の削除対象にならない
    dismissed_at: Mapped[str | None] = mapped_column(String, nullable=True)
```

`Article.__table_args__` にインデックスを追加する。

```python
        Index("idx_articles_genre", "genre"),
```

同じファイルの `Tag` クラスの直前に 2 モデルを追加する。

```python
class Genre(Base):
    """記事のジャンル定義。粒度は運用しながら変えるため DB に持つ。"""

    __tablename__ = "genres"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    label_ja: Mapped[str] = mapped_column(String, nullable=False)
    # 小さいほど優先。タグが複数ジャンルにヒットしたときの解決順
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    created_at: Mapped[str] = mapped_column(String, default=_utcnow)

    rules: Mapped[list["GenreRule"]] = relationship(
        back_populates="genre", cascade="all, delete-orphan"
    )


class GenreRule(Base):
    """タグ候補 1 語 → ジャンルの割り当て。"""

    __tablename__ = "genre_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tag: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    genre_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("genres.id", ondelete="CASCADE"), nullable=False
    )
    # True のルールは、通常ルールが 1 つも当たらなかったときだけ使う
    is_generic: Mapped[bool] = mapped_column(Boolean, default=False)

    genre: Mapped["Genre"] = relationship(back_populates="rules")
```

- [ ] **Step 4: シードを書く**

`backend/app/services/genre_seed.py` を新規作成する。

```python
"""ジャンル定義の初期値。genres テーブルが空のときだけ投入する。

実データ（未読 617 件）で分布を確認済み。dev 19% / ai 16% / politics 10% /
incident 8% / other 8% / science 7% / life 7% / culture 7% / economy 6% /
entertainment 6% / sports 3% / security 3%。

投入後はユーザーが管理画面で編集する前提なので、シードは二度と走らせない。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Genre, GenreRule

# (key, label_ja, priority, tags)
GENRE_SEED: list[tuple[str, str, int, list[str]]] = [
    ("ai", "AI・LLM", 1,
     ["ai", "llm", "openai", "claude", "rag", "mcp", "genai", "chatgpt", "gemini", "nvidia"]),
    ("security", "セキュリティ", 2,
     ["security", "privacy", "vulnerability", "malware"]),
    ("dev", "開発・技術", 3,
     ["programming", "web", "javascript", "python", "rust", "unity", "database", "api",
      "github", "linux", "windows", "microsoft", "software", "hardware", "network",
      "excel", "performance", "cloud", "aws", "vscode", "it", "tools", "data"]),
    ("sports", "スポーツ", 4,
     ["baseball", "sports", "sport", "soccer"]),
    ("incident", "事件・災害", 5,
     ["disaster", "accident", "earthquake", "crime", "safety"]),
    ("politics", "政治・行政", 6,
     ["government", "politics", "policy", "geopolitics", "law", "war",
      "local-government", "copyright", "gender", "labor", "disability"]),
    ("economy", "経済・ビジネス", 7,
     ["finance", "economy", "business", "tax", "yen", "accounting", "payment",
      "marketing", "retail", "consumer", "career", "monetization"]),
    ("science", "科学・教育", 8,
     ["research", "psychology", "education", "university", "mathematics", "medical",
      "agriculture", "wildlife", "logic", "infection", "space", "animal"]),
    ("culture", "文化・歴史", 9,
     ["history", "museum", "architecture", "art", "literature", "design",
      "writing", "media", "culture"]),
    ("entertainment", "エンタメ", 10,
     ["entertainment", "game", "manga", "anime", "movie", "music", "comedy",
      "story", "science-fiction", "comic", "book"]),
    ("life", "生活・健康", 11,
     ["health", "life", "lifestyle", "daily-life", "food", "recipe", "travel",
      "relationship", "emotion", "mental-health", "home", "weather",
      "society", "social", "community", "communication", "social-media",
      "railway", "transportation"]),
]

# 手がかりの弱いタグ。通常ルールが 1 つも当たらなかったときだけ使う。
# news / japan / japanese はどのジャンルにも寄せず、ルール無しのまま other に落とす
GENERIC_SEED: dict[str, str] = {"technology": "dev"}


async def seed_genres(session: AsyncSession) -> int:
    """genres が空のときだけ初期辞書を投入し、作成したジャンル数を返す。"""
    existing = await session.scalar(select(func.count()).select_from(Genre))
    if existing:
        return 0

    by_key: dict[str, Genre] = {}
    for key, label_ja, priority, tags in GENRE_SEED:
        genre = Genre(key=key, label_ja=label_ja, priority=priority)
        session.add(genre)
        by_key[key] = genre
    await session.flush()

    for key, _label, _priority, tags in GENRE_SEED:
        for tag in tags:
            session.add(GenreRule(tag=tag, genre_id=by_key[key].id, is_generic=False))
    for tag, key in GENERIC_SEED.items():
        session.add(GenreRule(tag=tag, genre_id=by_key[key].id, is_generic=True))
    await session.flush()

    return len(by_key)
```

- [ ] **Step 5: マイグレーションとシードを lifespan に組み込む**

`backend/app/main.py` の `PRAGMA table_info(articles)` ブロック（`main.py:75-93` 付近、`normalized_url` の追加と同じ場所）に追記する。

```python
        if "genre" not in existing_article_cols:
            await conn.execute(text("ALTER TABLE articles ADD COLUMN genre TEXT"))
        if "dismissed_at" not in existing_article_cols:
            await conn.execute(text("ALTER TABLE articles ADD COLUMN dismissed_at TEXT"))
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS idx_articles_genre ON articles(genre)")
        )
```

同じ lifespan 内、既存の `normalized_url` バックフィルの**直前**にシードを置く。Task 3 で足すジャンルのバックフィルより先に実行される位置であることが重要。

```python
    # ジャンル定義のシードは genre のバックフィルより必ず先に実行する。
    # 逆順だと空のルールで全件が genre="other" に確定し、
    # 「genre IS NULL」を条件とする以後のバックフィルで二度と拾えなくなる
    from app.services.genre_seed import seed_genres

    async with async_session() as session:
        created = await seed_genres(session)
        if created:
            await session.commit()
            logger.info("Seeded %d genres", created)
```

- [ ] **Step 6: テストを実行して通ることを確認**

Run: `cd backend && uv run pytest tests/test_genres_api.py -v`
Expected: PASS（2 tests）

Run: `cd backend && uv run pytest`
Expected: PASS（既存テストが壊れていないこと。特に `test_deduplicator.py` と `test_article_cleanup.py`）

- [ ] **Step 7: コミット**

```bash
git add backend/app/models.py backend/app/services/genre_seed.py backend/app/main.py
git commit -m "feat: add genre definition tables and seed"
```

---

### Task 3: ルール読み込み・再分類・ジャンル別未読件数 API

**Files:**
- Modify: `backend/app/services/genre_classifier.py`（`load_rules` / `reclassify_all` 追加）
- Modify: `backend/app/main.py`（genre バックフィル）
- Modify: `backend/app/routers/articles.py`（`GET /articles/genres`）
- Modify: `backend/app/schemas.py`（`GenreCountOut`）
- Test: `backend/tests/test_genres_api.py`（追記）

**Interfaces:**
- Consumes: `GenreRules` / `classify` / `OTHER_GENRE`（Task 1）、`Genre` / `GenreRule` / `Article.genre`（Task 2）
- Produces: `async load_rules(session) -> GenreRules`、`async reclassify_all(session) -> int`、`GET /api/articles/genres` → `list[GenreCountOut]`

- [ ] **Step 1: 失敗するテストを書く**

`backend/tests/test_genres_api.py` に追記する。ヘルパも同ファイルに置く。

```python
async def _make_feed(session, url: str = "https://example.com/feed"):
    from app.models import Feed

    feed = Feed(url=url, title="Test Feed")
    session.add(feed)
    await session.flush()
    return feed


async def _make_article(session, feed_id: int, guid: str, tags: list[str] | None, **kwargs):
    import json

    from app.models import Article

    article = Article(
        feed_id=feed_id,
        guid=guid,
        url=f"https://example.com/{guid}",
        title=kwargs.pop("title", "Title"),
        tag_suggestions=json.dumps(tags) if tags is not None else None,
        **kwargs,
    )
    session.add(article)
    await session.flush()
    return article


@pytest.mark.asyncio
async def test_genre_counts_group_unread_unsaved_articles(client: AsyncClient) -> None:
    from app.database import async_session
    from app.services.genre_classifier import reclassify_all

    async with async_session() as session:
        feed = await _make_feed(session)
        await _make_article(session, feed.id, "g1", ["llm"])
        await _make_article(session, feed.id, "g2", ["ai", "programming"])
        await _make_article(session, feed.id, "g3", ["baseball"])
        await _make_article(session, feed.id, "g4", ["llm"], is_read=True)   # 既読は数えない
        await _make_article(session, feed.id, "g5", ["llm"], is_saved=True)  # 保存済みも数えない
        await _make_article(session, feed.id, "g6", None)                    # 未分類は出さない
        await reclassify_all(session)
        await session.commit()

    res = await client.get("/api/articles/genres")
    assert res.status_code == 200
    counts = {row["genre"]: row["unread_count"] for row in res.json()}
    assert counts["ai"] == 2
    assert counts["sports"] == 1
    assert "other" not in counts

    labels = {row["genre"]: row["label_ja"] for row in res.json()}
    assert labels["ai"] == "AI・LLM"


@pytest.mark.asyncio
async def test_genre_counts_label_for_reserved_other(client: AsyncClient) -> None:
    from app.database import async_session
    from app.services.genre_classifier import reclassify_all

    async with async_session() as session:
        feed = await _make_feed(session)
        await _make_article(session, feed.id, "g1", ["working-holiday"])
        await reclassify_all(session)
        await session.commit()

    rows = res_json = (await client.get("/api/articles/genres")).json()
    other = next(r for r in rows if r["genre"] == "other")
    assert other["label_ja"] == "その他"
    assert res_json


@pytest.mark.asyncio
async def test_reclassify_all_returns_updated_count(client: AsyncClient) -> None:
    from app.database import async_session
    from app.models import Article
    from app.services.genre_classifier import reclassify_all
    from sqlalchemy import select

    async with async_session() as session:
        feed = await _make_feed(session)
        await _make_article(session, feed.id, "g1", ["llm"])
        await _make_article(session, feed.id, "g2", ["baseball"])
        changed = await reclassify_all(session)
        await session.commit()
        assert changed == 2

        genres = sorted((await session.execute(select(Article.genre))).scalars().all())
        assert genres == ["ai", "sports"]

    async with async_session() as session:
        # 変化が無ければ 0 件（毎回 UPDATE を投げない）
        assert await reclassify_all(session) == 0
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `cd backend && uv run pytest tests/test_genres_api.py -v`
Expected: FAIL — `ImportError: cannot import name 'reclassify_all'`

- [ ] **Step 3: `load_rules` と `reclassify_all` を実装**

`backend/app/services/genre_classifier.py` の末尾に追記する。ファイル先頭の import も更新する。

```python
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Article, Genre, GenreRule
```

```python
async def load_rules(session: AsyncSession) -> GenreRules:
    """genres / genre_rules から分類ルールのスナップショットを組み立てる。

    ルール表は 150 行程度と小さいのでキャッシュは持たない。多数の記事を回す
    ときだけ、呼び出し側がループの外で 1 回呼ぶこと。
    """
    rows = (
        await session.execute(
            select(GenreRule.tag, GenreRule.is_generic, Genre.key, Genre.priority).join(
                Genre, GenreRule.genre_id == Genre.id
            )
        )
    ).all()

    tag_to_genre: dict[str, str] = {}
    generic_to_genre: dict[str, str] = {}
    priority: dict[str, int] = {}
    for tag, is_generic, key, prio in rows:
        (generic_to_genre if is_generic else tag_to_genre)[tag] = key
        priority[key] = prio
    return GenreRules(tag_to_genre, generic_to_genre, priority)


def _parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        tags = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [t for t in tags if isinstance(t, str)]


async def reclassify_all(session: AsyncSession) -> int:
    """tag_suggestions を持つ全記事を分類し直し、変化した件数を返す。

    LLM を呼ばないので数千件でも一瞬で終わる。commit は呼び出し側が行う。
    """
    rules = await load_rules(session)
    articles = (
        await session.execute(select(Article).where(Article.tag_suggestions.isnot(None)))
    ).scalars().all()

    changed = 0
    for article in articles:
        genre = classify(_parse_tags(article.tag_suggestions), rules)
        if article.genre != genre:
            article.genre = genre
            changed += 1
    if changed:
        await session.flush()
    return changed
```

- [ ] **Step 4: バックフィルを lifespan に足す**

`backend/app/main.py`、Task 2 で足したシードの**直後**に置く。

```python
    # 未分類の記事にジャンルを埋める（シードの後でなければならない）
    from app.services.genre_classifier import reclassify_all

    async with async_session() as session:
        filled = await reclassify_all(session)
        if filled:
            await session.commit()
            logger.info("Backfilled genre for %d articles", filled)
```

- [ ] **Step 5: 件数 API を実装**

`backend/app/schemas.py` に追加する（`ExcludePatternOut` の後ろ）。

```python
class GenreCountOut(BaseModel):
    genre: str
    label_ja: str
    unread_count: int
```

`backend/app/routers/articles.py` に追加する。**`/articles/{article_id}` より前**に定義しないとパスが食われるので、`get_recommended_articles`（`articles.py:90`）の直前に置く。

```python
@router.get("/articles/genres", response_model=list[GenreCountOut])
async def get_genre_counts(session: AsyncSession = Depends(get_session)):
    """未読・未保存・未非表示の記事をジャンル別に数える。件数降順。"""
    from app.services.genre_classifier import OTHER_GENRE

    rows = (
        await session.execute(
            select(Article.genre, func.count().label("cnt"))
            .where(
                Article.is_read == False,  # noqa: E712
                Article.is_saved == False,  # noqa: E712
                Article.dismissed_at.is_(None),
                Article.genre.isnot(None),
            )
            .group_by(Article.genre)
            .order_by(func.count().desc())
        )
    ).all()

    labels = {
        key: label
        for key, label in (await session.execute(select(Genre.key, Genre.label_ja))).all()
    }
    labels[OTHER_GENRE] = "その他"
    return [
        GenreCountOut(genre=genre, label_ja=labels.get(genre, genre), unread_count=cnt)
        for genre, cnt in rows
    ]
```

`articles.py` 冒頭の import に `Genre` と `GenreCountOut` を足す。

- [ ] **Step 6: テストを実行して通ることを確認**

Run: `cd backend && uv run pytest tests/test_genres_api.py -v`
Expected: PASS（`test_seed_creates_initial_genres` はまだ `/api/genres` が無いので FAIL のまま。Task 4 で通る）

Run: `cd backend && uv run pytest tests/test_genres_api.py -k "count or reclassify" -v`
Expected: PASS（3 tests）

- [ ] **Step 7: コミット**

```bash
git add backend/app/services/genre_classifier.py backend/app/main.py \
        backend/app/routers/articles.py backend/app/schemas.py backend/tests/test_genres_api.py
git commit -m "feat: backfill article genres and expose counts"
```

---

### Task 4: ジャンル定義の CRUD API

**Files:**
- Create: `backend/app/routers/genres.py`
- Modify: `backend/app/schemas.py`、`backend/app/main.py`（ルータ登録）
- Test: `backend/tests/test_genres_api.py`（追記）

**Interfaces:**
- Consumes: `Genre` / `GenreRule`（Task 2）、`reclassify_all`（Task 3）
- Produces: `GET /api/genres`、`POST /api/genres`、`PATCH /api/genres/{id}`、`DELETE /api/genres/{id}`、`POST /api/genre-rules`、`DELETE /api/genre-rules/{id}`、`POST /api/articles/reclassify-genres`。変更系のレスポンスは `reclassified` を含む。

- [ ] **Step 1: 失敗するテストを書く**

`backend/tests/test_genres_api.py` に追記する。

```python
@pytest.mark.asyncio
async def test_create_genre_rejects_reserved_and_duplicate_key(client: AsyncClient) -> None:
    res = await client.post("/api/genres", json={"key": "other", "label_ja": "その他", "priority": 50})
    assert res.status_code == 400

    res = await client.post("/api/genres", json={"key": "ai", "label_ja": "重複", "priority": 50})
    assert res.status_code == 409

    res = await client.post("/api/genres", json={"key": "hobby", "label_ja": "趣味", "priority": 50})
    assert res.status_code == 201
    assert res.json()["key"] == "hobby"


@pytest.mark.asyncio
async def test_rule_moves_between_genres_instead_of_conflicting(client: AsyncClient) -> None:
    """既に他ジャンルにあるタグを送ったら 409 ではなく付け替える。"""
    genres = (await client.get("/api/genres")).json()
    sports_id = next(g["id"] for g in genres if g["key"] == "sports")

    res = await client.post("/api/genre-rules", json={"tag": "llm", "genre_id": sports_id, "is_generic": False})
    assert res.status_code == 201

    after = (await client.get("/api/genres")).json()
    tags = {g["key"]: [r["tag"] for r in g["rules"]] for g in after}
    assert "llm" in tags["sports"]
    assert "llm" not in tags["ai"]


@pytest.mark.asyncio
async def test_rule_change_reclassifies_existing_articles(client: AsyncClient) -> None:
    from app.database import async_session
    from app.services.genre_classifier import reclassify_all

    async with async_session() as session:
        feed = await _make_feed(session)
        await _make_article(session, feed.id, "g1", ["llm"])
        await reclassify_all(session)
        await session.commit()

    counts = {r["genre"]: r["unread_count"] for r in (await client.get("/api/articles/genres")).json()}
    assert counts == {"ai": 1}

    genres = (await client.get("/api/genres")).json()
    sports_id = next(g["id"] for g in genres if g["key"] == "sports")
    res = await client.post("/api/genre-rules", json={"tag": "llm", "genre_id": sports_id, "is_generic": False})
    assert res.json()["reclassified"] == 1

    counts = {r["genre"]: r["unread_count"] for r in (await client.get("/api/articles/genres")).json()}
    assert counts == {"sports": 1}


@pytest.mark.asyncio
async def test_delete_genre_removes_its_rules_and_reclassifies(client: AsyncClient) -> None:
    from app.database import async_session
    from app.services.genre_classifier import reclassify_all

    async with async_session() as session:
        feed = await _make_feed(session)
        await _make_article(session, feed.id, "g1", ["baseball"])
        await reclassify_all(session)
        await session.commit()

    genres = (await client.get("/api/genres")).json()
    sports_id = next(g["id"] for g in genres if g["key"] == "sports")

    res = await client.delete(f"/api/genres/{sports_id}")
    assert res.status_code == 200
    assert res.json()["reclassified"] == 1

    counts = {r["genre"]: r["unread_count"] for r in (await client.get("/api/articles/genres")).json()}
    assert counts == {"other": 1}


@pytest.mark.asyncio
async def test_patch_genre_updates_label_and_priority(client: AsyncClient) -> None:
    genres = (await client.get("/api/genres")).json()
    dev_id = next(g["id"] for g in genres if g["key"] == "dev")

    res = await client.patch(f"/api/genres/{dev_id}", json={"label_ja": "開発", "priority": 1})
    assert res.status_code == 200
    assert res.json()["label_ja"] == "開発"

    after = (await client.get("/api/genres")).json()
    assert next(g for g in after if g["key"] == "dev")["priority"] == 1
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `cd backend && uv run pytest tests/test_genres_api.py -v`
Expected: FAIL — `/api/genres` が 404

- [ ] **Step 3: スキーマを追加**

`backend/app/schemas.py` の `GenreCountOut` の下に追加する。

```python
class GenreRuleOut(BaseModel):
    id: int
    tag: str


class GenreOut(BaseModel):
    id: int
    key: str
    label_ja: str
    priority: int
    # 管理 UI がチップの削除に rule id を使うので、タグ名だけでなく id も返す
    rules: list[GenreRuleOut] = []
    generic_rules: list[GenreRuleOut] = []


class GenreCreate(BaseModel):
    key: str
    label_ja: str
    priority: int = 100


class GenreUpdate(BaseModel):
    label_ja: str | None = None
    priority: int | None = None


class GenreRuleCreate(BaseModel):
    tag: str
    genre_id: int
    is_generic: bool = False


class ReclassifyResult(BaseModel):
    reclassified: int
```

- [ ] **Step 4: ルータを実装**

`backend/app/routers/genres.py` を新規作成する。

```python
"""ジャンル定義の CRUD。

粒度と分け方は運用しながら変わるので、辞書はコード定数ではなく DB に置く。
変更のたびにその場で既存記事を再分類する（POST /exclude-patterns が追加時に
既存記事を purge するのと同じ作法）。LLM を呼ばないので数千件でも一瞬。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import Genre, GenreRule
from app.schemas import (
    GenreCreate,
    GenreOut,
    GenreRuleCreate,
    GenreRuleOut,
    GenreUpdate,
    ReclassifyResult,
)

router = APIRouter(tags=["genres"])

# genres テーブルに行を持たない予約キー（どのルールにも当たらない記事の受け皿）
_RESERVED_KEYS = {"other"}


async def _reclassify(session: AsyncSession) -> int:
    from app.services.genre_classifier import reclassify_all

    changed = await reclassify_all(session)
    await session.commit()
    return changed


async def _list_genres(session: AsyncSession) -> list[GenreOut]:
    genres = (
        await session.execute(select(Genre).order_by(Genre.priority, Genre.key))
    ).scalars().all()
    rules = (await session.execute(select(GenreRule))).scalars().all()

    by_genre: dict[int, tuple[list[GenreRuleOut], list[GenreRuleOut]]] = {
        g.id: ([], []) for g in genres
    }
    for rule in rules:
        normal, generic = by_genre.setdefault(rule.genre_id, ([], []))
        (generic if rule.is_generic else normal).append(GenreRuleOut(id=rule.id, tag=rule.tag))

    out: list[GenreOut] = []
    for genre in genres:
        normal, generic = by_genre[genre.id]
        out.append(
            GenreOut(
                id=genre.id,
                key=genre.key,
                label_ja=genre.label_ja,
                priority=genre.priority,
                rules=sorted(normal, key=lambda r: r.tag),
                generic_rules=sorted(generic, key=lambda r: r.tag),
            )
        )
    return out


@router.get("/genres", response_model=list[GenreOut])
async def list_genres(session: AsyncSession = Depends(get_session)):
    return await _list_genres(session)


@router.post("/genres", response_model=GenreOut, status_code=201)
async def create_genre(body: GenreCreate, session: AsyncSession = Depends(get_session)):
    key = body.key.strip().lower()
    if not key:
        raise HTTPException(status_code=400, detail="Key must not be empty")
    if key in _RESERVED_KEYS:
        raise HTTPException(status_code=400, detail=f"'{key}' is a reserved key")

    existing = await session.execute(select(Genre).where(Genre.key == key))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Genre already exists")

    genre = Genre(key=key, label_ja=body.label_ja.strip(), priority=body.priority)
    session.add(genre)
    await session.commit()
    await session.refresh(genre)
    return GenreOut(
        id=genre.id, key=genre.key, label_ja=genre.label_ja, priority=genre.priority
    )


@router.patch("/genres/{genre_id}", response_model=GenreOut)
async def update_genre(
    genre_id: int, body: GenreUpdate, session: AsyncSession = Depends(get_session)
):
    genre = await session.get(Genre, genre_id)
    if not genre:
        raise HTTPException(status_code=404, detail="Genre not found")
    if body.label_ja is not None:
        genre.label_ja = body.label_ja.strip()
    if body.priority is not None:
        genre.priority = body.priority
    await session.commit()
    # priority を変えると解決順が変わるので再分類する
    await _reclassify(session)
    return next(g for g in await _list_genres(session) if g.id == genre_id)


@router.delete("/genres/{genre_id}", response_model=ReclassifyResult)
async def delete_genre(genre_id: int, session: AsyncSession = Depends(get_session)):
    genre = await session.get(Genre, genre_id)
    if not genre:
        raise HTTPException(status_code=404, detail="Genre not found")
    await session.delete(genre)  # GenreRule は cascade で消える
    await session.commit()
    return ReclassifyResult(reclassified=await _reclassify(session))


@router.post("/genre-rules", response_model=ReclassifyResult, status_code=201)
async def create_genre_rule(
    body: GenreRuleCreate, session: AsyncSession = Depends(get_session)
):
    tag = body.tag.strip().lower()
    if not tag:
        raise HTTPException(status_code=400, detail="Tag must not be empty")
    if not await session.get(Genre, body.genre_id):
        raise HTTPException(status_code=404, detail="Genre not found")

    existing = (
        await session.execute(select(GenreRule).where(GenreRule.tag == tag))
    ).scalar_one_or_none()
    if existing:
        # 管理画面でタグを別ジャンルへ移す操作を自然にするため、衝突ではなく付け替え
        existing.genre_id = body.genre_id
        existing.is_generic = body.is_generic
    else:
        session.add(GenreRule(tag=tag, genre_id=body.genre_id, is_generic=body.is_generic))
    await session.commit()
    return ReclassifyResult(reclassified=await _reclassify(session))


@router.delete("/genre-rules/{rule_id}", response_model=ReclassifyResult)
async def delete_genre_rule(rule_id: int, session: AsyncSession = Depends(get_session)):
    rule = await session.get(GenreRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    await session.delete(rule)
    await session.commit()
    return ReclassifyResult(reclassified=await _reclassify(session))
```

`backend/app/routers/articles.py` に単独再分類のエンドポイントを足す（`regenerate-summaries` の近く）。

```python
@router.post("/articles/reclassify-genres", response_model=dict)
async def reclassify_genres(session: AsyncSession = Depends(get_session)):
    """辞書を直接いじった後などに全件を分類し直す。"""
    from app.services.genre_classifier import reclassify_all

    changed = await reclassify_all(session)
    await session.commit()
    return {"reclassified": changed}
```

`backend/app/main.py` にルータを登録する（`exclude_patterns` の次の行）。

```python
app.include_router(genres.router, prefix="/api")
```

同ファイル冒頭の `from app.routers import ...` に `genres` を追加する。

- [ ] **Step 5: テストを実行して通ることを確認**

Run: `cd backend && uv run pytest tests/test_genres_api.py -v`
Expected: PASS（全 9 tests）

- [ ] **Step 6: 全体テストで回帰が無いことを確認**

Run: `cd backend && uv run pytest`
Expected: PASS（既存テストを含む全件）

- [ ] **Step 7: コミット**

```bash
git add backend/app/routers/genres.py backend/app/routers/articles.py \
        backend/app/schemas.py backend/app/main.py backend/tests/test_genres_api.py
git commit -m "feat: add genre definition CRUD with instant reclassify"
```

---

### Task 5: 記事一覧のジャンルフィルタと分類の自動付与

**Files:**
- Modify: `backend/app/routers/articles.py:40-70`（`list_articles` に `genre`）
- Modify: `backend/app/services/background_processor.py:155`, `:219` 付近
- Test: `backend/tests/test_genres_api.py`（追記）

**Interfaces:**
- Consumes: `load_rules` / `classify`（Task 3）
- Produces: `GET /api/articles?genre=<key>`

- [ ] **Step 1: 失敗するテストを書く**

`backend/tests/test_genres_api.py` に追記する。

```python
@pytest.mark.asyncio
async def test_list_articles_filters_by_genre(client: AsyncClient) -> None:
    from app.database import async_session
    from app.services.genre_classifier import reclassify_all

    async with async_session() as session:
        feed = await _make_feed(session)
        await _make_article(session, feed.id, "g1", ["llm"], title="AI の記事")
        await _make_article(session, feed.id, "g2", ["baseball"], title="野球の記事")
        await reclassify_all(session)
        await session.commit()

    res = await client.get("/api/articles", params={"genre": "ai"})
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert data["items"][0]["title"] == "AI の記事"
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `cd backend && uv run pytest tests/test_genres_api.py::test_list_articles_filters_by_genre -v`
Expected: FAIL — `total == 2`（`genre` パラメータが無視される）

- [ ] **Step 3: `list_articles` にフィルタを足す**

`backend/app/routers/articles.py` の `list_articles` 引数に `genre: str | None = None` を追加し、`untagged` の分岐の直後に条件を足す。

```python
    if genre is not None:
        stmt = stmt.where(Article.genre == genre)
        count_stmt = count_stmt.where(Article.genre == genre)
```

- [ ] **Step 4: 背景処理で genre を設定**

`backend/app/services/background_processor.py`、`_process_phase1_one` の中で `tag_suggestions` を書いている箇所と同じトランザクションで `genre` も設定する。`_process_phase2_one` にも同じ処理を入れる。

```python
        # tag_suggestions を書くのと同じ場所でジャンルも決めておく
        from app.services.genre_classifier import classify, load_rules

        rules = await load_rules(session)
        article.genre = classify([en for en, _ja in pairs], rules)
```

（`pairs` は `summarize_and_tag` が返す `[(en, ja), ...]`。`tag_suggestions` に保存しているのと同じ英語タグ列を渡すこと。）

- [ ] **Step 5: テストを実行して通ることを確認**

Run: `cd backend && uv run pytest tests/test_genres_api.py -v`
Expected: PASS（10 tests）

- [ ] **Step 6: コミット**

```bash
git add backend/app/routers/articles.py backend/app/services/background_processor.py \
        backend/tests/test_genres_api.py
git commit -m "feat: filter articles by genre and set it during processing"
```

---

### Task 6: 非表示 API と各一覧からの除外

**Files:**
- Modify: `backend/app/routers/articles.py`（dismiss / undismiss、各一覧の除外、`mark-all-read` の `genre`）
- Modify: `backend/app/schemas.py`（`DismissRequest`、`ArticleOut.dismissed_at`、`MarkAllReadRequest.genre`）
- Test: `backend/tests/test_dismiss.py`（新規）

**Interfaces:**
- Consumes: `Article.dismissed_at`（Task 2）、`genre` フィルタ（Task 5）
- Produces: `POST /api/articles/dismiss` → `{"dismissed": int}`、`POST /api/articles/undismiss` → `{"restored": int}`、`GET /api/articles?dismissed=true`

- [ ] **Step 1: 失敗するテストを書く**

`backend/tests/test_dismiss.py` を新規作成する。`client` / `_make_feed` / `_make_article` は `tests/test_genres_api.py` と同じ内容をこのファイルにも書く（テストファイル間で共有しない既存方針に合わせる）。

```python
"""記事の非表示（dismissed）機能のテスト。

非表示は is_read を立てないため、article_cleanup の自動削除対象にならない。
"""

from __future__ import annotations

import importlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SNOREADER_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    from app import config as config_module

    config_module.settings = config_module.Settings()  # type: ignore[assignment]

    from app import database as database_module

    importlib.reload(database_module)

    from app import main as main_module

    importlib.reload(main_module)

    async with main_module.lifespan(main_module.app):
        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


async def _make_feed(session, url: str = "https://example.com/feed"):
    from app.models import Feed

    feed = Feed(url=url, title="Test Feed")
    session.add(feed)
    await session.flush()
    return feed


async def _make_article(session, feed_id: int, guid: str, tags: list[str] | None, **kwargs):
    from app.models import Article

    article = Article(
        feed_id=feed_id,
        guid=guid,
        url=f"https://example.com/{guid}",
        title=kwargs.pop("title", "Title"),
        tag_suggestions=json.dumps(tags) if tags is not None else None,
        **kwargs,
    )
    session.add(article)
    await session.flush()
    return article


async def _seed_articles(client: AsyncClient) -> None:
    from app.database import async_session
    from app.services.genre_classifier import reclassify_all

    async with async_session() as session:
        feed = await _make_feed(session)
        await _make_article(session, feed.id, "g1", ["baseball"], title="野球1")
        await _make_article(session, feed.id, "g2", ["soccer"], title="サッカー1")
        await _make_article(session, feed.id, "g3", ["llm"], title="AI1")
        await _make_article(session, feed.id, "g4", ["baseball"], title="保存野球", is_saved=True)
        await reclassify_all(session)
        await session.commit()


@pytest.mark.asyncio
async def test_dismiss_by_genre_hides_only_that_genre(client: AsyncClient) -> None:
    await _seed_articles(client)

    res = await client.post("/api/articles/dismiss", json={"genre": "sports"})
    assert res.status_code == 200
    assert res.json()["dismissed"] == 2  # 保存済みは対象外

    listed = (await client.get("/api/articles")).json()
    titles = {item["title"] for item in listed["items"]}
    assert titles == {"AI1", "保存野球"}


@pytest.mark.asyncio
async def test_dismiss_protects_saved_articles_by_ids(client: AsyncClient) -> None:
    from app.database import async_session
    from app.models import Article
    from sqlalchemy import select

    await _seed_articles(client)
    async with async_session() as session:
        saved_id = (
            await session.execute(select(Article.id).where(Article.is_saved == True))  # noqa: E712
        ).scalars().first()

    res = await client.post("/api/articles/dismiss", json={"ids": [saved_id]})
    assert res.json()["dismissed"] == 0


@pytest.mark.asyncio
async def test_dismissed_articles_excluded_from_lists(client: AsyncClient) -> None:
    await _seed_articles(client)
    await client.post("/api/articles/dismiss", json={"genre": "sports"})

    counts = {r["genre"]: r["unread_count"] for r in (await client.get("/api/articles/genres")).json()}
    assert "sports" not in counts

    feeds = (await client.get("/api/feeds")).json()
    assert feeds[0]["unread_count"] == 1  # AI1 のみ（保存済みは元から未読 0 扱いではない点に注意）

    unrec = (await client.get("/api/articles/unrecommended")).json()
    assert all("野球" not in item["title"] for item in unrec["items"])


@pytest.mark.asyncio
async def test_dismissed_articles_visible_in_search(client: AsyncClient) -> None:
    await _seed_articles(client)
    await client.post("/api/articles/dismiss", json={"genre": "sports"})

    res = await client.get("/api/articles/search", params={"q": "野球"})
    titles = {item["title"] for item in res.json()["items"]}
    assert "野球1" in titles
    assert next(i for i in res.json()["items"] if i["title"] == "野球1")["dismissed_at"] is not None


@pytest.mark.asyncio
async def test_undismiss_restores_articles(client: AsyncClient) -> None:
    await _seed_articles(client)
    await client.post("/api/articles/dismiss", json={"genre": "sports"})

    res = await client.post("/api/articles/undismiss", json={"genre": "sports"})
    assert res.json()["restored"] == 2

    listed = (await client.get("/api/articles")).json()
    assert listed["total"] == 4


@pytest.mark.asyncio
async def test_dismissed_view_lists_only_dismissed(client: AsyncClient) -> None:
    await _seed_articles(client)
    await client.post("/api/articles/dismiss", json={"genre": "sports"})

    res = await client.get("/api/articles", params={"dismissed": "true"})
    titles = {item["title"] for item in res.json()["items"]}
    assert titles == {"野球1", "サッカー1"}


@pytest.mark.asyncio
async def test_dismiss_requires_genre_or_ids(client: AsyncClient) -> None:
    res = await client.post("/api/articles/dismiss", json={})
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_mark_all_read_by_genre_protects_saved(client: AsyncClient) -> None:
    await _seed_articles(client)

    res = await client.post("/api/articles/mark-all-read", json={"genre": "sports"})
    assert res.json()["marked"] == 2  # 保存済みの「保存野球」は既読にしない

    saved = (await client.get("/api/articles", params={"is_saved": "true"})).json()
    assert saved["items"][0]["is_read"] is False
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `cd backend && uv run pytest tests/test_dismiss.py -v`
Expected: FAIL — `/api/articles/dismiss` が 404

- [ ] **Step 3: スキーマを追加・変更**

`backend/app/schemas.py`:

```python
class DismissRequest(BaseModel):
    genre: str | None = None
    ids: list[int] | None = None
```

`ArticleOut` に 1 行足す。

```python
    dismissed_at: str | None = None
```

`MarkAllReadRequest` に 1 行足す。

```python
    genre: str | None = None
```

- [ ] **Step 4: エンドポイントを実装**

`backend/app/routers/articles.py` に追加する。

```python
def _dismiss_targets(body: DismissRequest, *, restoring: bool):
    """dismiss / undismiss の対象を絞る WHERE 条件を組む。"""
    conds = []
    if body.ids:
        conds.append(Article.id.in_(body.ids))
    elif body.genre:
        conds.append(Article.genre == body.genre)
    if restoring:
        conds.append(Article.dismissed_at.isnot(None))
    else:
        # 保存済みは常に保護する。誤って束で捨てても資料が消えないようにするため
        conds.append(Article.is_saved == False)  # noqa: E712
        conds.append(Article.dismissed_at.is_(None))
    return conds


@router.post("/articles/dismiss", response_model=dict)
async def dismiss_articles(
    body: DismissRequest, session: AsyncSession = Depends(get_session)
):
    """記事を一覧から外す。is_read は変えないので自動削除の対象にならない。"""
    if not body.ids and not body.genre:
        raise HTTPException(status_code=422, detail="Either genre or ids is required")

    now = datetime.now(timezone.utc).isoformat()
    articles = (
        await session.execute(select(Article).where(*_dismiss_targets(body, restoring=False)))
    ).scalars().all()
    for article in articles:
        article.dismissed_at = now
    await session.commit()
    return {"dismissed": len(articles)}


@router.post("/articles/undismiss", response_model=dict)
async def undismiss_articles(
    body: DismissRequest, session: AsyncSession = Depends(get_session)
):
    if not body.ids and not body.genre:
        raise HTTPException(status_code=422, detail="Either genre or ids is required")

    articles = (
        await session.execute(select(Article).where(*_dismiss_targets(body, restoring=True)))
    ).scalars().all()
    for article in articles:
        article.dismissed_at = None
    await session.commit()
    return {"restored": len(articles)}
```

`list_articles` に `dismissed: bool = False` 引数を足し、`genre` 条件の直後に置く。

```python
    # 通常の読書導線からは外し、Dismissed ビューでだけ見せる
    if dismissed:
        stmt = stmt.where(Article.dismissed_at.isnot(None))
        count_stmt = count_stmt.where(Article.dismissed_at.isnot(None))
    else:
        stmt = stmt.where(Article.dismissed_at.is_(None))
        count_stmt = count_stmt.where(Article.dismissed_at.is_(None))
```

Dismissed ビューは「最近捨てたものから」見たいので、`# Sort` ブロック（`articles.py:70-73`）の並び替えを差し替える。`sort_col` を決めている行の直後に置くこと（後から `order_by` を足しても既存の指定が優先されるため、分岐で列そのものを切り替える）。

```python
    sort_col = Article.dismissed_at if dismissed else sort_col
```

`get_recommended_articles`（`articles.py:129` 付近）と `get_unrecommended_articles`（`articles.py:196` 付近）の `stmt` の `where` に 1 行ずつ足す。この 2 つは `list_articles` とは別関数なので個別対応が必要。

```python
            Article.dismissed_at.is_(None),
```

`get_genre_counts` には Task 3 で既に条件が入っている。

`mark_all_read`（`articles.py:513`）に `genre` 対応を足す。

```python
    if body.genre is not None:
        # 一括 dismiss が保存済みを保護する以上、genre 一括だけ保護しないのは非対称
        stmt = stmt.where(
            Article.genre == body.genre,
            Article.is_saved == False,  # noqa: E712
            Article.dismissed_at.is_(None),
        )
```

`backend/app/routers/feeds.py:20` の未読数サブクエリに条件を足す。

```python
        .where(Article.is_read == False, Article.dismissed_at.is_(None))  # noqa: E712
```

（既存の `where` に `Article.dismissed_at.is_(None)` を追加する形にすること。）

- [ ] **Step 5: テストを実行して通ることを確認**

Run: `cd backend && uv run pytest tests/test_dismiss.py -v`
Expected: PASS（8 tests）

- [ ] **Step 6: 全体テストで回帰が無いことを確認**

Run: `cd backend && uv run pytest`
Expected: PASS

- [ ] **Step 7: コミット**

```bash
git add backend/app/routers/articles.py backend/app/routers/feeds.py \
        backend/app/schemas.py backend/tests/test_dismiss.py
git commit -m "feat: add dismiss state for bulk-hiding articles"
```

---

### Task 7: 重複統合で非表示状態が消えないようにする

**Files:**
- Modify: `backend/app/services/deduplicator.py:99-131`（`_merge_into_keeper`）
- Test: `backend/tests/test_deduplicator.py`（追記）

**Interfaces:**
- Consumes: `Article.dismissed_at` / `Article.genre`（Task 2）、`load_rules` / `classify`（Task 3）
- Produces: なし（既存関数の挙動修正）

**なぜ必要か:** `_merge_into_keeper` は引き継ぐフィールドを明示列挙しており、`dismissed_at` を足さないと dedup のたびに非表示状態が落ちる。dedup は毎フェッチ後に自動実行され、生存優先順位は「非はてなブックマーク由来を優先」。非表示対象の多くははてブ経由なので、同じ URL が元サイトのフィードから来ると keeper が非はてブ側になり、**非表示にしたはずの記事が未読で復活する**。

- [ ] **Step 1: 失敗するテストを書く**

`backend/tests/test_deduplicator.py` の末尾に追記する。

```python
@pytest.mark.asyncio
async def test_dedup_keeps_dismissed_state(client: AsyncClient) -> None:
    """はてブ側で非表示にした記事が、元サイト側の生存で復活しないこと。"""
    from app.database import async_session
    from app.models import Article
    from sqlalchemy import select

    async with async_session() as session:
        hatena_feed = await _make_feed(session, "https://b.hatena.ne.jp/hotentry/it.rss")
        normal_feed = await _make_feed(session, "https://normal.example.com/feed")
        await _make_article(
            session, hatena_feed.id, "g1", "https://news.example.com/story",
            dismissed_at="2026-08-08T00:00:00+00:00",
        )
        await _make_article(session, normal_feed.id, "g2", "https://news.example.com/story")
        await session.commit()

    res = await client.post("/api/articles/dedup", json={"dry_run": False})
    assert res.json()["deleted"] == 1

    async with async_session() as session:
        remaining = (await session.execute(select(Article))).scalars().all()
        assert len(remaining) == 1
        assert remaining[0].dismissed_at is not None


@pytest.mark.asyncio
async def test_dedup_recomputes_genre_on_keeper(client: AsyncClient) -> None:
    """loser の tag_suggestions を引き継いだ keeper のジャンルが計算し直されること。"""
    import json

    from app.database import async_session
    from app.models import Article
    from sqlalchemy import select

    async with async_session() as session:
        hatena_feed = await _make_feed(session, "https://b.hatena.ne.jp/hotentry/it.rss")
        normal_feed = await _make_feed(session, "https://normal.example.com/feed")
        await _make_article(
            session, hatena_feed.id, "g1", "https://news.example.com/story",
            tag_suggestions=json.dumps(["baseball"]),
        )
        await _make_article(session, normal_feed.id, "g2", "https://news.example.com/story")
        await session.commit()

    await client.post("/api/articles/dedup", json={"dry_run": False})

    async with async_session() as session:
        keeper = (await session.execute(select(Article))).scalars().one()
        assert keeper.genre == "sports"
```

- [ ] **Step 2: テストを実行して失敗を確認**

Run: `cd backend && uv run pytest tests/test_deduplicator.py -k dismissed -v`
Expected: FAIL — `assert remaining[0].dismissed_at is not None` が None で落ちる

- [ ] **Step 3: `_merge_into_keeper` を修正**

`backend/app/services/deduplicator.py` の `_merge_into_keeper` に追記する。`is_saved` の処理の直後に置く。

```python
    # 片方でも非表示なら非表示のままにする。ここで引き継がないと、
    # 毎フェッチ後の dedup で非表示にしたはずの記事が未読に戻る
    if loser.dismissed_at and not keeper.dismissed_at:
        keeper.dismissed_at = loser.dismissed_at
```

`("content", "ai_summary", "tag_suggestions", "image_url")` のループの直後に、ジャンルの再計算を足す。

```python
    # tag_suggestions が loser 由来に差し替わることがあるので、ジャンルは計算し直す
    from app.services.genre_classifier import classify, load_rules

    rules = await load_rules(session)
    tags: list[str] = []
    if keeper.tag_suggestions:
        try:
            tags = [t for t in json.loads(keeper.tag_suggestions) if isinstance(t, str)]
        except (ValueError, TypeError):
            tags = []
    keeper.genre = classify(tags, rules)
```

`deduplicator.py` の先頭に `import json` が無ければ追加する。

- [ ] **Step 4: テストを実行して通ることを確認**

Run: `cd backend && uv run pytest tests/test_deduplicator.py -v`
Expected: PASS（既存分を含む全件）

- [ ] **Step 5: コミット**

```bash
git add backend/app/services/deduplicator.py backend/tests/test_deduplicator.py
git commit -m "fix: preserve dismissed state through article dedup"
```

---

### Task 8: フロントエンド — 型と API クライアントとフック

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/hooks/useArticles.ts`
- Create: `frontend/src/hooks/useGenres.ts`

**Interfaces:**
- Consumes: Task 3〜6 の API
- Produces: `useGenreCounts()`, `useDismiss()`, `useUndismiss()`, `useGenres()`, `useCreateGenre()`, `useUpdateGenre()`, `useDeleteGenre()`, `useCreateGenreRule()`, `useDeleteGenreRule()`

- [ ] **Step 1: 型を追加**

`frontend/src/types.ts`:

```ts
export interface GenreCount {
  genre: string;
  label_ja: string;
  unread_count: number;
}

export interface GenreRuleDef {
  id: number;
  tag: string;
}

export interface GenreDef {
  id: number;
  key: string;
  label_ja: string;
  priority: number;
  rules: GenreRuleDef[];
  generic_rules: GenreRuleDef[];
}
```

`ArticleFilters` に 2 行、`Article` に 1 行足す。

```ts
  genre?: string;
  dismissed?: boolean;
```

```ts
  dismissed_at: string | null;
```

- [ ] **Step 2: API クライアントを追加**

`frontend/src/api/client.ts` の既存関数群に合わせて追加する。

```ts
export const getGenreCounts = () => fetchJSON<GenreCount[]>('/articles/genres');

export const dismissArticles = (body: { genre?: string; ids?: number[] }) =>
  fetchJSON<{ dismissed: number }>('/articles/dismiss', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

export const undismissArticles = (body: { genre?: string; ids?: number[] }) =>
  fetchJSON<{ restored: number }>('/articles/undismiss', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

export const getGenres = () => fetchJSON<GenreDef[]>('/genres');

export const createGenre = (body: { key: string; label_ja: string; priority: number }) =>
  fetchJSON<GenreDef>('/genres', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

export const updateGenre = (id: number, body: { label_ja?: string; priority?: number }) =>
  fetchJSON<GenreDef>(`/genres/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

export const deleteGenre = (id: number) =>
  fetchJSON<{ reclassified: number }>(`/genres/${id}`, { method: 'DELETE' });

export const createGenreRule = (body: { tag: string; genre_id: number; is_generic: boolean }) =>
  fetchJSON<{ reclassified: number }>('/genre-rules', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

export const deleteGenreRule = (id: number) =>
  fetchJSON<{ reclassified: number }>(`/genre-rules/${id}`, { method: 'DELETE' });
```

`getArticles` が `ArticleFilters` を query に載せている箇所で、`genre` と `dismissed` も送るようにする（既存の `feed_id` などと同じ扱い）。

- [ ] **Step 3: フックを追加**

`frontend/src/hooks/useArticles.ts` に追加する。invalidate 対象は既存の `useMarkAllRead`（`useArticles.ts:54`）に揃える。dismissed は Recommend / Unrecommend からも除外されるので、これを落とすとサイドバーのバッジが古いまま残る。

```ts
export function useGenreCounts() {
  return useQuery({
    queryKey: ['genre-counts'],
    queryFn: api.getGenreCounts,
    staleTime: 30_000,
  });
}

// 一括操作は影響範囲が広いので、既存の in-place マージではなく invalidate する
function useInvalidateAfterBulk() {
  const qc = useQueryClient();
  return () => {
    qc.invalidateQueries({ queryKey: ['articles'] });
    qc.invalidateQueries({ queryKey: ['genre-counts'] });
    qc.invalidateQueries({ queryKey: ['feeds'] });
    qc.invalidateQueries({ queryKey: ['recommended-count'] });
    qc.invalidateQueries({ queryKey: ['unrecommended-count'] });
  };
}

export function useDismiss() {
  const invalidate = useInvalidateAfterBulk();
  return useMutation({
    mutationFn: (body: { genre?: string; ids?: number[] }) => api.dismissArticles(body),
    onSuccess: invalidate,
  });
}

export function useUndismiss() {
  const invalidate = useInvalidateAfterBulk();
  return useMutation({
    mutationFn: (body: { genre?: string; ids?: number[] }) => api.undismissArticles(body),
    onSuccess: invalidate,
  });
}
```

`frontend/src/hooks/useGenres.ts` を新規作成する。クエリキーは `['genres']`（記事側の集計 `['genre-counts']` と取り違えないため別語にする）。

```ts
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import * as api from '../api/client';

export function useGenres() {
  return useQuery({ queryKey: ['genres'], queryFn: api.getGenres, staleTime: 60_000 });
}

// ジャンル定義を変えると既存記事が再分類されるので、記事側のキャッシュも捨てる
function useInvalidateGenreDefs() {
  const qc = useQueryClient();
  return () => {
    qc.invalidateQueries({ queryKey: ['genres'] });
    qc.invalidateQueries({ queryKey: ['genre-counts'] });
    qc.invalidateQueries({ queryKey: ['articles'] });
  };
}

export function useCreateGenre() {
  const invalidate = useInvalidateGenreDefs();
  return useMutation({
    mutationFn: (body: { key: string; label_ja: string; priority: number }) => api.createGenre(body),
    onSuccess: invalidate,
  });
}

export function useUpdateGenre() {
  const invalidate = useInvalidateGenreDefs();
  return useMutation({
    mutationFn: ({ id, ...body }: { id: number; label_ja?: string; priority?: number }) =>
      api.updateGenre(id, body),
    onSuccess: invalidate,
  });
}

export function useDeleteGenre() {
  const invalidate = useInvalidateGenreDefs();
  return useMutation({ mutationFn: (id: number) => api.deleteGenre(id), onSuccess: invalidate });
}

export function useCreateGenreRule() {
  const invalidate = useInvalidateGenreDefs();
  return useMutation({
    mutationFn: (body: { tag: string; genre_id: number; is_generic: boolean }) =>
      api.createGenreRule(body),
    onSuccess: invalidate,
  });
}

export function useDeleteGenreRule() {
  const invalidate = useInvalidateGenreDefs();
  return useMutation({ mutationFn: (id: number) => api.deleteGenreRule(id), onSuccess: invalidate });
}
```

- [ ] **Step 4: 型チェックが通ることを確認**

Run: `cd frontend && npm run build`
Expected: `✓ built`（tsc がエラーを出さない）

- [ ] **Step 5: lint がベースラインより悪化していないことを確認**

Run: `cd frontend && npm run lint 2>&1 | tail -2`
Expected: エラー件数が 19 を超えないこと

- [ ] **Step 6: コミット**

```bash
git add frontend/src/types.ts frontend/src/api/client.ts \
        frontend/src/hooks/useArticles.ts frontend/src/hooks/useGenres.ts
git commit -m "feat: add genre and dismiss API client hooks"
```

---

### Task 9: フロントエンド — サイドバーのジャンル表示と管理画面

**Files:**
- Modify: `frontend/src/components/layout/FeedSidebar.tsx`

**Interfaces:**
- Consumes: Task 8 のフック
- Produces: `filters.genre` / `filters.dismissed` を設定するナビゲーション

- [ ] **Step 1: ジャンルセクションを追加**

`FeedSidebar.tsx`、フィード一覧の直前に置く。既存のフィード行と同じ見た目にする。件数 0 のジャンルは API が返さないので、フィルタ不要。

```tsx
{genreCounts && genreCounts.length > 0 && (
  <div className="mt-4">
    <div className="px-2 mb-1 text-xs font-semibold text-gray-400">ジャンル</div>
    {genreCounts.map((g) => (
      <button
        key={g.genre}
        onClick={() => onFilterChange({
          ...filters, genre: g.genre, dismissed: undefined,
          feed_id: undefined, is_saved: undefined, tag_id: undefined, untagged: undefined,
          recommended: undefined, unrecommended: undefined, extract_failed: undefined,
        })}
        className={`w-full flex items-center gap-2 px-2 py-1 text-sm text-left rounded hover:bg-gray-100 dark:hover:bg-gray-800 ${
          filters.genre === g.genre ? 'bg-gray-200 dark:bg-gray-800 font-semibold' : ''
        }`}
      >
        <span className="truncate flex-1">{g.label_ja}</span>
        <span className="text-xs bg-blue-500 text-white rounded-full px-1.5 py-0.5 min-w-[20px] text-center shrink-0">
          {g.unread_count}
        </span>
      </button>
    ))}
  </div>
)}
```

- [ ] **Step 2: Dismissed ビューへの導線を追加**

ジャンルセクションの直後に置く。

```tsx
<button
  onClick={() => onFilterChange({
    ...filters, dismissed: true, genre: undefined,
    feed_id: undefined, is_saved: undefined, tag_id: undefined, untagged: undefined,
    recommended: undefined, unrecommended: undefined, extract_failed: undefined,
  })}
  className={`w-full px-2 py-1 text-sm text-left rounded hover:bg-gray-100 dark:hover:bg-gray-800 ${
    filters.dismissed ? 'bg-gray-200 dark:bg-gray-800 font-semibold' : ''
  }`}
>
  非表示にした記事
</button>
```

- [ ] **Step 3: ジャンル管理モーダルを追加**

既存の `除外パターン管理` と同じ位置・同じ開閉の作法で `ジャンル管理` を足す。モーダル内は次を持つ。

```tsx
{genres?.map((g) => (
  <div key={g.id} className="border-b border-gray-200 dark:border-gray-700 py-2">
    <div className="flex items-center gap-2">
      <input
        defaultValue={g.label_ja}
        onBlur={(e) => {
          const v = e.target.value.trim();
          if (v && v !== g.label_ja) updateGenre.mutate({ id: g.id, label_ja: v });
        }}
        className="text-sm px-1 py-0.5 border rounded dark:bg-gray-800 dark:border-gray-600"
      />
      <span className="text-xs text-gray-400 font-mono">{g.key}</span>
      <div className="flex-1" />
      <button
        onClick={() => updateGenre.mutate({ id: g.id, priority: g.priority - 1 })}
        className="text-xs px-1.5 py-0.5 rounded border border-gray-300 dark:border-gray-600"
        title="優先順位を上げる（複数ジャンルに当たったとき勝ちやすくなる）"
      >↑</button>
      <button
        onClick={() => updateGenre.mutate({ id: g.id, priority: g.priority + 1 })}
        className="text-xs px-1.5 py-0.5 rounded border border-gray-300 dark:border-gray-600"
        title="優先順位を下げる"
      >↓</button>
      <button
        onClick={() => {
          if (!confirm(`ジャンル「${g.label_ja}」を削除しますか？\n所属タグの割り当ても消え、記事は再分類されます。`)) return;
          deleteGenre.mutate(g.id);
        }}
        className="text-xs px-1.5 py-0.5 rounded border border-red-300 text-red-600 dark:border-red-700 dark:text-red-400"
      >削除</button>
    </div>
    <div className="mt-1 flex flex-wrap gap-1">
      {g.rules.map((r) => (
        <span key={r.id} className="inline-flex items-center gap-0.5 px-1.5 py-0.5 text-xs bg-gray-100 dark:bg-gray-800 rounded">
          {r.tag}
          <button onClick={() => deleteRule.mutate(r.id, { onSuccess: (res) => setLastReclassified(res.reclassified) })} className="text-gray-400 hover:text-red-500">×</button>
        </span>
      ))}
      {g.generic_rules.map((r) => (
        <span key={r.id} className="inline-flex items-center gap-0.5 px-1.5 py-0.5 text-xs border border-dashed border-gray-400 rounded" title="汎用ルール: 他に手がかりが無いときだけ使う">
          {r.tag}
          <button onClick={() => deleteRule.mutate(r.id, { onSuccess: (res) => setLastReclassified(res.reclassified) })} className="text-gray-400 hover:text-red-500">×</button>
        </span>
      ))}
    </div>
  </div>
))}
```

タグ追加フォームを一覧の下に置く。既に他ジャンルに割り当て済みのタグはバックエンドが付け替えるので、フロント側で重複チェックはしない。

```tsx
<form
  onSubmit={(e) => {
    e.preventDefault();
    const tag = newTag.trim().toLowerCase();
    if (!tag || newRuleGenreId == null) return;
    createRule.mutate(
      { tag, genre_id: newRuleGenreId, is_generic: newIsGeneric },
      {
        onSuccess: (res) => { setLastReclassified(res.reclassified); setNewTag(''); },
        onError: (err) => { alert((err as Error).message); },
      },
    );
  }}
  className="mt-2 flex items-center gap-1.5 flex-wrap"
>
  <select
    value={newRuleGenreId ?? ''}
    onChange={(e) => setNewRuleGenreId(Number(e.target.value))}
    className="text-sm px-1 py-0.5 border rounded dark:bg-gray-800 dark:border-gray-600"
  >
    {genres?.map((g) => <option key={g.id} value={g.id}>{g.label_ja}</option>)}
  </select>
  <input
    value={newTag}
    onChange={(e) => setNewTag(e.target.value)}
    placeholder="タグ (英小文字)"
    className="text-sm px-1.5 py-0.5 border rounded dark:bg-gray-800 dark:border-gray-600"
  />
  <label className="text-xs text-gray-500 flex items-center gap-1">
    <input type="checkbox" checked={newIsGeneric} onChange={(e) => setNewIsGeneric(e.target.checked)} />
    汎用
  </label>
  <button type="submit" disabled={createRule.isPending} className="text-xs text-blue-500 hover:text-blue-700 disabled:opacity-50">
    追加
  </button>
</form>
{lastReclassified !== null && (
  <p className="mt-1 text-xs text-gray-500">{lastReclassified} 件を分類し直しました</p>
)}
```

必要な state は次の 4 つ。

```tsx
const [newTag, setNewTag] = useState('');
const [newRuleGenreId, setNewRuleGenreId] = useState<number | null>(null);
const [newIsGeneric, setNewIsGeneric] = useState(false);
const [lastReclassified, setLastReclassified] = useState<number | null>(null);
```

ジャンル新規作成フォームも同じモーダルに置く。

```tsx
<form
  onSubmit={(e) => {
    e.preventDefault();
    const key = newGenreKey.trim().toLowerCase();
    if (!key || !newGenreLabel.trim()) return;
    createGenre.mutate(
      { key, label_ja: newGenreLabel.trim(), priority: 100 },
      {
        onSuccess: () => { setNewGenreKey(''); setNewGenreLabel(''); },
        onError: (err) => { alert((err as Error).message); },
      },
    );
  }}
  className="mt-3 flex items-center gap-1.5"
>
  <input value={newGenreKey} onChange={(e) => setNewGenreKey(e.target.value)} placeholder="key (英小文字)"
    className="text-sm px-1.5 py-0.5 border rounded dark:bg-gray-800 dark:border-gray-600 w-32" />
  <input value={newGenreLabel} onChange={(e) => setNewGenreLabel(e.target.value)} placeholder="表示名"
    className="text-sm px-1.5 py-0.5 border rounded dark:bg-gray-800 dark:border-gray-600 w-32" />
  <button type="submit" className="text-xs text-blue-500 hover:text-blue-700">ジャンル追加</button>
</form>
```

`other` を見に行く導線も置く。辞書を育てるループがこの画面で閉じる。

```tsx
<button
  onClick={() => { setShowGenreManager(false); onFilterChange({ ...filters, genre: 'other', dismissed: undefined }); }}
  className="text-xs text-blue-500 hover:text-blue-700"
>
  分類できなかった記事（その他）を見る
</button>
```

- [ ] **Step 4: `GenreDef` の型を API に合わせる**

`frontend/src/types.ts` の `GenreDef`（Task 8 で追加済み）の `rules` / `generic_rules` は、Task 4 の `GenreOut` が返す `{id, tag}` 配列と一致させる。

```ts
export interface GenreRuleDef {
  id: number;
  tag: string;
}

export interface GenreDef {
  id: number;
  key: string;
  label_ja: string;
  priority: number;
  rules: GenreRuleDef[];
  generic_rules: GenreRuleDef[];
}
```

- [ ] **Step 5: ビルドと lint**

Run: `cd frontend && npm run build`
Expected: `✓ built`

Run: `cd frontend && npm run lint 2>&1 | tail -2`
Expected: エラー件数が 19 を超えない

- [ ] **Step 6: コミット**

```bash
git add frontend/src/components/layout/FeedSidebar.tsx frontend/src/types.ts
git commit -m "feat: add genre nav and management UI to sidebar"
```

---

### Task 10: フロントエンド — 一括操作と Undo と非表示バッジ

**Files:**
- Modify: `frontend/src/components/articles/ArticleList.tsx`

**Interfaces:**
- Consumes: `useDismiss` / `useUndismiss` / `useMarkAllRead`（Task 8）
- Produces: なし（最終段）

- [ ] **Step 1: 一括操作ボタンを追加**

`ArticleList.tsx` のツールバー、`filters.genre` が設定されているときだけ出す。**検索中は無効化する** — `useSearchArticles`（`useArticles.ts:143`）は `{ feed_id, is_saved }` しか受け取らず `genre` を渡せない独立モードなので、絞り込んだ数件のつもりで押すとそのジャンルの全未読が処理されてしまう。

```tsx
{filters.genre && !filters.dismissed && (
  <>
    <button
      disabled={isSearching || dismiss.isPending}
      title={isSearching ? '検索中は使えません（検索の絞り込みは一括操作に反映されません）' : 'このジャンルの未読をまとめて既読にする'}
      onClick={() => {
        if (!confirm(`「${genreLabel}」の未読 ${total} 件をまとめて既読にしますか？`)) return;
        markAllRead.mutate({ genre: filters.genre });
      }}
      className="text-xs text-blue-500 hover:text-blue-700 disabled:opacity-40"
    >
      まとめて既読
    </button>
    <button
      disabled={isSearching || dismiss.isPending}
      title={isSearching ? '検索中は使えません（検索の絞り込みは一括操作に反映されません）' : 'このジャンルの未読を一覧から外す（削除はされません）'}
      onClick={() => {
        if (!confirm(`「${genreLabel}」の未読 ${total} 件を非表示にしますか？\n削除はされません。「非表示にした記事」から戻せます。`)) return;
        dismiss.mutate({ genre: filters.genre }, {
          onSuccess: (r) => setLastDismissed({ genre: filters.genre!, count: r.dismissed }),
        });
      }}
      className="text-xs text-gray-500 hover:text-gray-700 disabled:opacity-40"
    >
      まとめて非表示
    </button>
  </>
)}
```

- [ ] **Step 2: Undo 表示を追加**

ツールバーの直下に置く。一括操作は多数の記事の扱いを一度に決めるので、事前の `confirm()` だけでなく事後の撤回経路を持たせる。

```tsx
{lastDismissed && (
  <div className="px-2 py-1 flex items-center gap-2 text-xs bg-gray-100 dark:bg-gray-800">
    <span>{lastDismissed.count} 件を非表示にしました</span>
    <button
      onClick={() => {
        undismiss.mutate({ genre: lastDismissed.genre });
        setLastDismissed(null);
      }}
      className="text-blue-500 hover:text-blue-700"
    >
      元に戻す
    </button>
  </div>
)}
```

`lastDismissed` は `useState<{ genre: string; count: number } | null>(null)`。フィルタ変更時にクリアする（既存の `useEffect(..., [filters, searchQuery])` に `setLastDismissed(null)` を足す）。

- [ ] **Step 3: Dismissed ビューの「まとめて戻す」を追加**

```tsx
{filters.dismissed && total > 0 && (
  <button
    onClick={() => {
      const ids = displayArticles.map(a => a.id);
      if (!confirm(`表示中の ${ids.length} 件を元に戻しますか？`)) return;
      undismiss.mutate({ ids });
    }}
    className="text-xs text-blue-500 hover:text-blue-700"
  >
    まとめて戻す
  </button>
)}
```

- [ ] **Step 4: 「非表示」バッジを追加**

検索結果・抽出失敗一覧に dismissed が混ざるため、カードに小さなバッジを出す。`ExtractStatusBadge` の下に同じ作りで定義する。

```tsx
function DismissedBadge({ dismissedAt }: { dismissedAt: string | null | undefined }) {
  if (!dismissedAt) return null;
  return (
    <span className="text-xs px-1.5 py-0.5 rounded font-mono bg-gray-200 text-gray-600 dark:bg-gray-700 dark:text-gray-300">
      非表示
    </span>
  );
}
```

`displayArticles.map` の中、`ArticleCard` の直後に置く（`isExtractFailedView` のバッジ行と同じ位置づけ）。

```tsx
{article.dismissed_at && (
  <div className="px-3 pb-1 -mt-1">
    <DismissedBadge dismissedAt={article.dismissed_at} />
  </div>
)}
```

- [ ] **Step 5: ビルドと lint**

Run: `cd frontend && npm run build`
Expected: `✓ built`

Run: `cd frontend && npm run lint 2>&1 | tail -2`
Expected: エラー件数が 19 を超えない

- [ ] **Step 6: バックエンド全体テスト**

Run: `cd backend && uv run pytest`
Expected: PASS

- [ ] **Step 7: 実機確認**

`make deploy` してから `launchctl kickstart -k gui/501/com.ccxa.snoreader` で再起動し、新しい PID を控える。ブラウザで次を確認する。

1. サイドバーにジャンルが件数付きで並ぶ
2. ジャンルを選ぶと記事が絞り込まれる
3. 「まとめて非表示」で一覧から消え、サイドバーの件数とフィードの未読バッジが減る
4. 「元に戻す」で戻る
5. 「非表示にした記事」に出る
6. 検索すると非表示の記事も「非表示」バッジ付きで出る
7. ジャンル管理でタグを別ジャンルへ移すと、再分類件数が表示され一覧が変わる

- [ ] **Step 8: コミット**

```bash
git add frontend/src/components/articles/ArticleList.tsx
git commit -m "feat: add bulk genre triage controls with undo"
```

---

## リリース手順

全タスク完了後:

1. `backend/pyproject.toml` と `frontend/package.json` のバージョンを揃えて上げる（現在 0.9.45 → 0.10.0。機能追加なので minor）
2. `README.md` と `README.ja.md` の機能一覧にジャンル別トリアージを追記する
3. コミットしてタグ `v0.10.0` を打ち、`git push origin main --tags`
4. `make deploy` + `launchctl kickstart -k gui/501/com.ccxa.snoreader`、新 PID と `/api/feeds` の 200 応答を確認
