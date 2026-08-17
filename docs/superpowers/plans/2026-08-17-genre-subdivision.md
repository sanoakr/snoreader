# ジャンルのサブジャンル分割 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ジャンルに 1 段の子階層を持たせ、親の未読が 30 件を超えたときだけサイドバーで子を展開して、ひとつの束を「片付けられる大きさ」に見せる。

**Architecture:** `genres` に `parent_id` を足すだけで済ませる。記事側は既存の `Article.genre` 1 列に**葉のキー**を入れ、親の件数は集計時に子の合計として導出するので、記事テーブルのスキーマ変更とデータ移行は不要。分類は従来どおり決定的な辞書写像で、解決規則に「祖先と子孫が両方当たったら子孫が勝つ」を足す。親を指すフィルタは子孫キーの集合に展開する。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.0 (async) / SQLite (WAL + FTS5) / pytest + pytest-asyncio / React 19 + TypeScript + TanStack Query + Tailwind v4

**Spec:** `docs/superpowers/specs/2026-08-17-genre-subdivision-design.md`

## Global Constraints

- 作業フローは `AGENTS.md` に従う（ブランチ → 実装 → テスト → PR → `--no-ff` マージ → バージョン更新 → タグ → README 更新 → push → LaunchAgent 再起動）。**main への直接コミットは禁止。**
- 本計画のブランチは `feat/genre-subdivision`（spec のコミットで既に作成済み）。
- 閾値は **30**。定数名は バックエンド不要・フロント `GENRE_SPLIT_THRESHOLD = 30`（`FeedSidebar.tsx` に 1 箇所だけ置く）。
- 階層は **2 段固定**。子を親に指定する作成・更新は HTTP 400。
- コメントは日本語、識別子は英語。マジックナンバーは定数化。
- ルーターから `app.ai.*` / `app.services.*` を呼ぶときは**関数内 import**（既存の慣習）。`app.models` / `app.database` / `app.schemas` は通常のトップレベル import。
- LLM は一切関与しない。テストは LLM をモックする必要すらない。
- バックエンドテスト: `cd backend && .venv/bin/python -m pytest`。フロント型チェック: `cd frontend && npx tsc --noEmit`。
- 予約キー `"other"` は `genres` テーブルに行を持たない。親でも子でもなく、常にトップレベル 1 行として扱う。
- `technology` は `is_generic=True` の汎用ルール。子へ移すときも generic のまま。

---

### Task 1: `genres.parent_id` 列とマイグレーション

**Files:**
- Modify: `backend/app/models.py:83-97`（`Genre`）
- Modify: `backend/app/main.py:78-103`（lifespan の手動 `ALTER TABLE` 群）
- Test: `backend/tests/test_genre_hierarchy.py`（新規）

**Interfaces:**
- Consumes: なし（最初のタスク）
- Produces: `Genre.parent_id: Mapped[int | None]`、`Genre.children: Mapped[list[Genre]]`、`Genre.parent: Mapped[Genre | None]`

- [ ] **Step 1: 失敗するテストを書く**

`backend/tests/test_genre_hierarchy.py` を新規作成する。fixture は `tests/test_genres_api.py:14-34` と同じ形（テストごとに tmp_path の DB を作り、lifespan を通す）。

```python
"""ジャンルの親子階層のテスト。

階層は 2 段固定で、Article.genre は葉のキーを持つ。親の件数は子の合計として
集計時に導出するので、記事側のスキーマ変更は無い。
"""

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
async def test_genres_table_has_parent_id_column(client: AsyncClient) -> None:
    """create_all は既存テーブルを変更しないので、手動 ALTER TABLE が必要。"""
    from sqlalchemy import text

    from app.database import engine

    async with engine.connect() as conn:
        rows = (await conn.execute(text("PRAGMA table_info(genres)"))).fetchall()
    assert "parent_id" in {row[1] for row in rows}


@pytest.mark.asyncio
async def test_child_genre_links_to_parent(client: AsyncClient) -> None:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.database import async_session
    from app.models import Genre

    async with async_session() as session:
        parent = (await session.execute(select(Genre).where(Genre.key == "ai"))).scalar_one()
        session.add(Genre(key="ai_llm", label_ja="LLM・生成AI", priority=1, parent_id=parent.id))
        await session.commit()

    async with async_session() as session:
        parent = (
            await session.execute(
                select(Genre).options(selectinload(Genre.children)).where(Genre.key == "ai")
            )
        ).scalar_one()
        assert [c.key for c in parent.children] == ["ai_llm"]


@pytest.mark.asyncio
async def test_deleting_parent_deletes_children(client: AsyncClient) -> None:
    from sqlalchemy import func, select

    from app.database import async_session
    from app.models import Genre

    async with async_session() as session:
        parent = (await session.execute(select(Genre).where(Genre.key == "ai"))).scalar_one()
        session.add(Genre(key="ai_llm", label_ja="LLM・生成AI", priority=1, parent_id=parent.id))
        await session.commit()

    async with async_session() as session:
        parent = (await session.execute(select(Genre).where(Genre.key == "ai"))).scalar_one()
        await session.delete(parent)
        await session.commit()
        remaining = await session.scalar(
            select(func.count()).select_from(Genre).where(Genre.key == "ai_llm")
        )
        assert remaining == 0
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_genre_hierarchy.py -v`
Expected: FAIL。`test_genres_table_has_parent_id_column` は `parent_id` が無く AssertionError、他 2 件は `Genre(parent_id=...)` が `TypeError: 'parent_id' is an invalid keyword argument`。

- [ ] **Step 3: モデルに列と関係を足す**

`backend/app/models.py` の `Genre` を次のようにする（既存の `rules` 関係はそのまま残す）。

```python
class Genre(Base):
    """記事のジャンル定義。粒度は運用しながら変えるため DB に持つ。"""

    __tablename__ = "genres"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    label_ja: Mapped[str] = mapped_column(String, nullable=False)
    # 小さいほど優先。タグが複数ジャンルにヒットしたときの解決順
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # NULL が親ジャンル。値を持つものが子。階層は 2 段固定（API 側で検証する）
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("genres.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[str] = mapped_column(String, default=_utcnow)

    rules: Mapped[list["GenreRule"]] = relationship(
        back_populates="genre", cascade="all, delete-orphan"
    )
    # 親を消したら子も消える。SQLite の外部キー制約だけに頼らず ORM 側でも伝播させる
    children: Mapped[list["Genre"]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )
    parent: Mapped["Genre | None"] = relationship(
        back_populates="children", remote_side="Genre.id"
    )
```

- [ ] **Step 4: lifespan にマイグレーションを足す**

`backend/app/main.py` の `articles` の `ALTER TABLE` 群のすぐ後（`idx_articles_genre` を作っている箇所の直後）に追加する。

```python
        # genres の親子階層（create_all は既存テーブルに列を足さない）
        genre_col_rows = await conn.execute(text("PRAGMA table_info(genres)"))
        existing_genre_cols = {row[1] for row in genre_col_rows.fetchall()}
        if "parent_id" not in existing_genre_cols:
            await conn.execute(text("ALTER TABLE genres ADD COLUMN parent_id INTEGER"))
```

`ALTER TABLE ... ADD COLUMN` は SQLite では外部キー制約を後付けできないので、参照整合性は ORM の cascade と API のバリデーションで担保する（既存の `articles` の追加列と同じ割り切り）。

- [ ] **Step 5: テストが通ることを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_genre_hierarchy.py -v`
Expected: 3 件 PASS

- [ ] **Step 6: 既存テストが壊れていないことを確認**

Run: `cd backend && .venv/bin/python -m pytest -q`
Expected: 既存の全件 + 新規 3 件が PASS

- [ ] **Step 7: コミット**

```bash
git add backend/app/models.py backend/app/main.py backend/tests/test_genre_hierarchy.py
git commit -m "feat: add parent_id to genres for one level of subgenres"
```

---

### Task 2: 分類器の子孫優先

**Files:**
- Modify: `backend/app/services/genre_classifier.py`（`GenreRules`, `classify`, `load_rules`）
- Test: `backend/tests/test_genre_classifier.py`（既存に追記）

**Interfaces:**
- Consumes: Task 1 の `Genre.parent_id`
- Produces: `GenreRules(tag_to_genre, generic_to_genre, priority, parent)` — `parent: dict[str, str | None]` は「子 key → 親 key」。親ジャンルは値 `None` を持つ形でも良いが、**キーが無い場合も親として扱う**（既存の呼び出し側が 3 引数で組んでいるテストを壊さないため `parent` は既定値 `{}` を持つ）

- [ ] **Step 1: 失敗するテストを書く**

`backend/tests/test_genre_classifier.py` の末尾に追記する。既存 fixture `rules` は 3 引数で `GenreRules` を組んでいるので、階層用の fixture を別に足す。

```python
@pytest.fixture
def hierarchical_rules() -> GenreRules:
    """ai(親) の下に ai_llm(子)、dev(親) の下に dev_general(子・汎用) を置いた構成。"""
    return GenreRules(
        tag_to_genre={
            "ai": "ai",            # 親を指す代表タグ
            "llm": "ai_llm",       # 子を指すタグ
            "programming": "dev",
            "baseball": "sports",
        },
        generic_to_genre={"technology": "dev_general"},
        priority={"ai": 1, "ai_llm": 1, "dev": 3, "dev_general": 3, "sports": 4},
        parent={"ai_llm": "ai", "dev_general": "dev"},
    )


def test_descendant_beats_ancestor(hierarchical_rules: GenreRules):
    """代表タグ(ai)と子タグ(llm)が両方当たったら子を採る。

    priority の手動調整に頼ると、代表タグを持つ記事が親に残り続けて分割されない。
    """
    assert classify(["ai", "llm"], hierarchical_rules) == "ai_llm"
    assert classify(["llm", "ai"], hierarchical_rules) == "ai_llm"


def test_parent_kept_when_no_child_rule_hits(hierarchical_rules: GenreRules):
    """子ルールが無いタグの記事は親に残る（親自身の束になる）。"""
    assert classify(["ai"], hierarchical_rules) == "ai"


def test_unrelated_genres_still_resolve_by_priority(hierarchical_rules: GenreRules):
    """祖先・子孫の関係が無い候補どうしは従来通り priority で決まる。"""
    assert classify(["ai", "programming"], hierarchical_rules) == "ai"
    assert classify(["baseball", "programming"], hierarchical_rules) == "dev"


def test_child_genre_wins_over_unrelated_higher_priority(hierarchical_rules: GenreRules):
    """子に降ろしても、親と同じ priority を与えていれば他ジャンルとの優劣は変わらない。"""
    assert classify(["llm", "programming"], hierarchical_rules) == "ai_llm"


def test_generic_stage_also_prunes_ancestors():
    """汎用ルールの段でも子孫優先が効くこと。"""
    rules = GenreRules(
        tag_to_genre={},
        generic_to_genre={"technology": "dev_general", "news": "dev"},
        priority={"dev": 3, "dev_general": 3},
        parent={"dev_general": "dev"},
    )
    assert classify(["technology", "news"], rules) == "dev_general"


def test_other_is_not_part_of_the_hierarchy(hierarchical_rules: GenreRules):
    assert classify(["unknown-tag"], hierarchical_rules) == "other"
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_genre_classifier.py -v`
Expected: FAIL。`GenreRules() got an unexpected keyword argument 'parent'`

- [ ] **Step 3: `GenreRules` に `parent` を足して枝刈りを実装**

`backend/app/services/genre_classifier.py` の `GenreRules` と `_resolve` / `classify` を置き換える。

```python
@dataclass(frozen=True)
class GenreRules:
    """DB から組み立てた分類ルールのスナップショット。"""

    tag_to_genre: dict[str, str]      # 通常ルール: tag -> genre key
    generic_to_genre: dict[str, str]  # 汎用ルール: tag -> genre key
    priority: dict[str, int]          # genre key -> priority（小さいほど優先）
    # 子 key -> 親 key。キーを持たないジャンルは親（またはトップレベル）扱い
    parent: dict[str, str] = field(default_factory=dict)

    def ancestors(self, genre: str) -> set[str]:
        """genre の祖先キー集合。階層は 2 段だが、循環しても止まるよう辿る。"""
        seen: set[str] = set()
        current = self.parent.get(genre)
        while current and current not in seen:
            seen.add(current)
            current = self.parent.get(current)
        return seen


def _prune_ancestors(hits: list[str], rules: GenreRules) -> list[str]:
    """候補の中に祖先と子孫が混在していたら祖先を落とす。

    「より具体的な指定が勝つ」という規則。これが無いと、親を指す代表タグ
    （ai など）を持つ記事が子ルールに当たっても親に残り、分割されない。
    """
    covered: set[str] = set()
    for genre in hits:
        covered |= rules.ancestors(genre)
    pruned = [g for g in hits if g not in covered]
    # 全滅（循環など異常なケース）のときは元の候補で解決する
    return pruned or hits


def _resolve(genres: list[str], rules: GenreRules) -> str:
    """候補ジャンルから priority 最小のものを返す。同値は key の辞書順で決める。"""
    candidates = _prune_ancestors(genres, rules)
    return min(candidates, key=lambda g: (rules.priority.get(g, _FALLBACK_PRIORITY), g))
```

`classify` 本体は変更不要（両段が `_resolve` を通るため枝刈りが両方に効く）。`field` を import に足す。

```python
from dataclasses import dataclass, field
```

- [ ] **Step 4: `load_rules` が親子を読むようにする**

同ファイルの `load_rules` を置き換える。ルールを持たない親も `parent` の解決に必要なので、`genres` は別途全件読む。

```python
async def load_rules(session: AsyncSession) -> GenreRules:
    """genres / genre_rules から分類ルールのスナップショットを組み立てる。

    ルール表は 150 行程度と小さいのでキャッシュは持たない。多数の記事を回す
    ときだけ、呼び出し側がループの外で 1 回呼ぶこと。
    """
    genre_rows = (await session.execute(select(Genre.id, Genre.key, Genre.parent_id))).all()
    key_by_id = {gid: key for gid, key, _parent in genre_rows}
    parent: dict[str, str] = {}
    for _gid, key, parent_id in genre_rows:
        parent_key = key_by_id.get(parent_id) if parent_id is not None else None
        if parent_key:
            parent[key] = parent_key

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
    # ルールを持たない親も priority を引けるようにする
    for _gid, key, _parent_id in genre_rows:
        priority.setdefault(key, _FALLBACK_PRIORITY)
    return GenreRules(tag_to_genre, generic_to_genre, priority, parent)
```

`priority.setdefault` は「ルールを持たない親」が `_resolve` で不利にならないための保険。ルールが無い＝候補に上がらないので実際には効かないが、`priority` を参照する将来の呼び出しで KeyError を出さない。

- [ ] **Step 5: テストが通ることを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_genre_classifier.py -v`
Expected: 既存 17 件 + 新規 6 件が PASS

- [ ] **Step 6: 全体テスト**

Run: `cd backend && .venv/bin/python -m pytest -q`
Expected: 全件 PASS

- [ ] **Step 7: コミット**

```bash
git add backend/app/services/genre_classifier.py backend/tests/test_genre_classifier.py
git commit -m "feat: resolve descendant genres before their ancestors"
```

---

### Task 3: genre スコープの展開ヘルパと `GET /articles`

**Files:**
- Create: `backend/app/services/genre_scope.py`
- Modify: `backend/app/routers/articles.py:43-78`（`list_articles`）
- Test: `backend/tests/test_genre_hierarchy.py`（追記）

**Interfaces:**
- Consumes: Task 1 の `Genre.parent_id`
- Produces: `async def genre_keys(session: AsyncSession, genre: str, *, exact: bool = False) -> list[str]` — `genre` とその子孫のキー一覧（`exact=True` なら `[genre]`）。`genres` に無いキー（`"other"` や削除済みジャンル）は `[genre]` を返す

新しいファイルに切るのは、同じ展開を `list_articles` / `mark_all_read` / `dismiss` の 3 箇所が共有する必要があり、`articles.py` は既に 1000 行超で最大のルーターだから。

- [ ] **Step 1: 失敗するテストを書く**

`backend/tests/test_genre_hierarchy.py` に追記する。

```python
async def _seed_hierarchy() -> None:
    """ai の下に ai_llm を作り、llm タグを子に付け替える。"""
    from sqlalchemy import select

    from app.database import async_session
    from app.models import Genre, GenreRule

    async with async_session() as session:
        parent = (await session.execute(select(Genre).where(Genre.key == "ai"))).scalar_one()
        child = Genre(key="ai_llm", label_ja="LLM・生成AI", priority=1, parent_id=parent.id)
        session.add(child)
        await session.flush()
        rule = (
            await session.execute(select(GenreRule).where(GenreRule.tag == "llm"))
        ).scalar_one()
        rule.genre_id = child.id
        await session.commit()


async def _make_article(guid: str, genre: str, **kwargs) -> int:
    from sqlalchemy import select

    from app.database import async_session
    from app.models import Article, Feed

    async with async_session() as session:
        feed = (await session.execute(select(Feed))).scalars().first()
        if feed is None:
            feed = Feed(url="https://example.com/feed", title="Test Feed")
            session.add(feed)
            await session.flush()
        article = Article(
            feed_id=feed.id,
            guid=guid,
            url=f"https://example.com/{guid}",
            title=kwargs.pop("title", "Title"),
            summary="",
            genre=genre,
            **kwargs,
        )
        session.add(article)
        await session.flush()
        await session.commit()
        return article.id


@pytest.mark.asyncio
async def test_genre_keys_expands_to_descendants(client: AsyncClient) -> None:
    from app.database import async_session
    from app.services.genre_scope import genre_keys

    await _seed_hierarchy()
    async with async_session() as session:
        assert sorted(await genre_keys(session, "ai")) == ["ai", "ai_llm"]
        assert await genre_keys(session, "ai_llm") == ["ai_llm"]
        assert await genre_keys(session, "ai", exact=True) == ["ai"]
        # genres に行を持たない予約キーはそのまま返す
        assert await genre_keys(session, "other") == ["other"]


@pytest.mark.asyncio
async def test_list_articles_by_parent_includes_children(client: AsyncClient) -> None:
    await _seed_hierarchy()
    await _make_article("p1", "ai")
    await _make_article("c1", "ai_llm")

    resp = await client.get("/api/articles?genre=ai")
    assert resp.status_code == 200
    assert {a["guid"] for a in resp.json()["items"]} == {"p1", "c1"}
    assert resp.json()["total"] == 2


@pytest.mark.asyncio
async def test_list_articles_by_child_returns_only_child(client: AsyncClient) -> None:
    await _seed_hierarchy()
    await _make_article("p1", "ai")
    await _make_article("c1", "ai_llm")

    resp = await client.get("/api/articles?genre=ai_llm")
    assert {a["guid"] for a in resp.json()["items"]} == {"c1"}


@pytest.mark.asyncio
async def test_list_articles_genre_exact_excludes_children(client: AsyncClient) -> None:
    """子を持つ親の「まだ子ルールが無いタグの記事」を単独で扱う導線。"""
    await _seed_hierarchy()
    await _make_article("p1", "ai")
    await _make_article("c1", "ai_llm")

    resp = await client.get("/api/articles?genre=ai&genre_exact=true")
    assert {a["guid"] for a in resp.json()["items"]} == {"p1"}
    assert resp.json()["total"] == 1
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_genre_hierarchy.py -v`
Expected: FAIL。`ModuleNotFoundError: No module named 'app.services.genre_scope'`、および `genre=ai` が親キー完全一致のため `total == 1` になる。

- [ ] **Step 3: 展開ヘルパを作る**

`backend/app/services/genre_scope.py` を新規作成する。

```python
"""genre 指定をキー集合へ展開する。

親ジャンルの指定は「その親と全ての子孫」を意味する。一覧・一括既読・一括
dismiss の 3 箇所が同じ意味で動く必要があるため、展開はここ 1 箇所に置く。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Genre


async def genre_keys(
    session: AsyncSession, genre: str, *, exact: bool = False
) -> list[str]:
    """genre とその子孫のキー一覧を返す。

    - ``exact=True``: そのキーだけ（子を持つ親の直下だけを対象にしたいとき）
    - ``genres`` に行を持たないキー（予約キー ``"other"`` や削除済みジャンル）は
      そのまま 1 件で返す
    """
    if exact:
        return [genre]

    rows = (await session.execute(select(Genre.id, Genre.key, Genre.parent_id))).all()
    id_by_key = {key: gid for gid, key, _parent in rows}
    if genre not in id_by_key:
        return [genre]

    children_by_parent: dict[int, list[str]] = {}
    for _gid, key, parent_id in rows:
        if parent_id is not None:
            children_by_parent.setdefault(parent_id, []).append(key)

    keys = [genre]
    queue = [id_by_key[genre]]
    while queue:
        for child_key in children_by_parent.get(queue.pop(), []):
            if child_key in keys:
                continue  # 循環していても止まる
            keys.append(child_key)
            queue.append(id_by_key[child_key])
    return keys
```

- [ ] **Step 4: `GET /articles` に `genre_exact` を足して展開を使う**

`backend/app/routers/articles.py` の `list_articles` シグネチャに引数を足す（`genre` の直後）。

```python
    genre: str | None = None,
    genre_exact: bool = False,
```

`genre` の絞り込みを置き換える。

```python
    if genre is not None:
        from app.services.genre_scope import genre_keys

        keys = await genre_keys(session, genre, exact=genre_exact)
        stmt = stmt.where(Article.genre.in_(keys))
        count_stmt = count_stmt.where(Article.genre.in_(keys))
```

- [ ] **Step 5: テストが通ることを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_genre_hierarchy.py -v`
Expected: 追記した 4 件を含め全件 PASS

- [ ] **Step 6: 全体テスト**

Run: `cd backend && .venv/bin/python -m pytest -q`
Expected: 全件 PASS（`tests/test_genres_api.py::test_list_articles_filters_by_genre` が既存の意味のまま通ること）

- [ ] **Step 7: コミット**

```bash
git add backend/app/services/genre_scope.py backend/app/routers/articles.py backend/tests/test_genre_hierarchy.py
git commit -m "feat: expand parent genre filter to descendants"
```

---

### Task 4: 一括既読 / 一括 dismiss の genre スコープ

**Files:**
- Modify: `backend/app/schemas.py:104-106`（`MarkAllReadRequest`）と `DismissRequest`
- Modify: `backend/app/routers/articles.py:565-612`（`mark_all_read`, `_dismiss_targets`）
- Test: `backend/tests/test_genre_hierarchy.py`（追記）

**Interfaces:**
- Consumes: Task 3 の `genre_keys`
- Produces: `MarkAllReadRequest.genre_exact: bool = False`、`DismissRequest.genre_exact: bool = False`。`_dismiss_targets(body, *, restoring, keys)` — `keys` は展開済みキー一覧（`None` なら genre 指定なし）

- [ ] **Step 1: 失敗するテストを書く**

`backend/tests/test_genre_hierarchy.py` に追記する。

```python
@pytest.mark.asyncio
async def test_mark_all_read_by_parent_covers_children(client: AsyncClient) -> None:
    await _seed_hierarchy()
    await _make_article("p1", "ai")
    await _make_article("c1", "ai_llm")

    resp = await client.post("/api/articles/mark-all-read", json={"genre": "ai"})
    assert resp.json()["marked"] == 2

    listed = await client.get("/api/articles?genre=ai&is_read=false")
    assert listed.json()["total"] == 0


@pytest.mark.asyncio
async def test_mark_all_read_genre_exact_leaves_children(client: AsyncClient) -> None:
    await _seed_hierarchy()
    await _make_article("p1", "ai")
    await _make_article("c1", "ai_llm")

    resp = await client.post(
        "/api/articles/mark-all-read", json={"genre": "ai", "genre_exact": True}
    )
    assert resp.json()["marked"] == 1

    listed = await client.get("/api/articles?genre=ai_llm&is_read=false")
    assert listed.json()["total"] == 1


@pytest.mark.asyncio
async def test_dismiss_by_parent_covers_children(client: AsyncClient) -> None:
    await _seed_hierarchy()
    await _make_article("p1", "ai")
    await _make_article("c1", "ai_llm")

    resp = await client.post("/api/articles/dismiss", json={"genre": "ai"})
    assert resp.json()["dismissed"] == 2
    assert len(resp.json()["ids"]) == 2


@pytest.mark.asyncio
async def test_dismiss_genre_exact_leaves_children(client: AsyncClient) -> None:
    await _seed_hierarchy()
    await _make_article("p1", "ai")
    await _make_article("c1", "ai_llm")

    resp = await client.post(
        "/api/articles/dismiss", json={"genre": "ai", "genre_exact": True}
    )
    assert resp.json()["dismissed"] == 1

    listed = await client.get("/api/articles?genre=ai_llm")
    assert listed.json()["total"] == 1
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_genre_hierarchy.py -k "mark_all_read or dismiss" -v`
Expected: FAIL。親指定が完全一致のままなので `marked == 1` / `dismissed == 1` になる。

- [ ] **Step 3: スキーマに `genre_exact` を足す**

`backend/app/schemas.py` の 2 つのリクエストモデルに追加する。

```python
class MarkAllReadRequest(BaseModel):
    feed_id: int | None = None
    genre: str | None = None
    # True にすると子ジャンルを含めず、その genre 直下の記事だけを対象にする
    genre_exact: bool = False
```

`DismissRequest` にも同じ 1 行（`genre_exact: bool = False`）を足す。

- [ ] **Step 4: ルーターで展開を使う**

`mark_all_read` の genre 節を置き換える。

```python
    if body.genre is not None:
        from app.services.genre_scope import genre_keys

        # 一括 dismiss が保存済みを保護する以上、genre 一括だけ保護しないのは非対称
        keys = await genre_keys(session, body.genre, exact=body.genre_exact)
        stmt = stmt.where(
            Article.genre.in_(keys),
            Article.is_saved == False,  # noqa: E712
        )
```

`_dismiss_targets` は同期関数なので、展開結果を引数で受け取る形にする。

```python
def _dismiss_targets(body: DismissRequest, *, restoring: bool, keys: list[str] | None):
    """dismiss / undismiss の対象を絞る WHERE 条件を組む。

    keys は genre を子孫まで展開したキー一覧（genre 指定が無ければ None）。
    """
    conds = []
    if body.ids:
        conds.append(Article.id.in_(body.ids))
    elif keys:
        conds.append(Article.genre.in_(keys))
        if not restoring:
            # UI の確認ダイアログは「未読 N 件」の unread_count を見せているので、
            # 実処理も未読に限定しないと確認件数と実処理件数がずれる（既読混入で桁違いになる）
            conds.append(Article.is_read == False)  # noqa: E712
    if restoring:
        conds.append(Article.dismissed_at.isnot(None))
    else:
        # 保存済みは常に保護する。誤って束で捨てても資料が消えないようにするため
        conds.append(Article.is_saved == False)  # noqa: E712
        conds.append(Article.dismissed_at.is_(None))
    return conds
```

呼び出し側 2 箇所（dismiss / undismiss）で、展開してから渡す。

```python
    keys = (
        await genre_keys(session, body.genre, exact=body.genre_exact)
        if body.genre
        else None
    )
    conds = _dismiss_targets(body, restoring=False, keys=keys)
```

`from app.services.genre_scope import genre_keys` は各関数の先頭で行う（関数内 import の慣習）。

- [ ] **Step 5: テストが通ることを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_genre_hierarchy.py -v`
Expected: 全件 PASS

- [ ] **Step 6: 既存の dismiss テストが壊れていないことを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_dismiss.py tests/test_genres_api.py -q`
Expected: 全件 PASS

- [ ] **Step 7: コミット**

```bash
git add backend/app/schemas.py backend/app/routers/articles.py backend/tests/test_genre_hierarchy.py
git commit -m "feat: scope bulk read/dismiss to a genre and its descendants"
```

---

### Task 5: 階層化した件数 API

**Files:**
- Modify: `backend/app/schemas.py:138-141`（`GenreCountOut`）
- Modify: `backend/app/routers/articles.py:108-135`（`get_genre_counts`）
- Test: `backend/tests/test_genre_hierarchy.py`（追記）

**Interfaces:**
- Consumes: Task 1 の `Genre.parent_id`
- Produces: `GenreCountOut(genre: str, label_ja: str, unread_count: int, direct_count: int, children: list[GenreCountOut])`。`unread_count` は `direct_count` + 子の合計

- [ ] **Step 1: 失敗するテストを書く**

```python
@pytest.mark.asyncio
async def test_genre_counts_nest_children_and_sum_parent(client: AsyncClient) -> None:
    await _seed_hierarchy()
    await _make_article("p1", "ai")
    await _make_article("c1", "ai_llm")
    await _make_article("c2", "ai_llm")

    rows = (await client.get("/api/articles/genres")).json()
    ai = next(r for r in rows if r["genre"] == "ai")
    assert ai["unread_count"] == 3
    assert ai["direct_count"] == 1
    assert [(c["genre"], c["unread_count"]) for c in ai["children"]] == [("ai_llm", 2)]


@pytest.mark.asyncio
async def test_genre_counts_parent_appears_even_with_no_direct_articles(
    client: AsyncClient,
) -> None:
    """代表タグを子に降ろすと親の直下は 0 件になる。それでも親は一覧に出る。"""
    await _seed_hierarchy()
    await _make_article("c1", "ai_llm")

    rows = (await client.get("/api/articles/genres")).json()
    ai = next(r for r in rows if r["genre"] == "ai")
    assert ai["direct_count"] == 0
    assert ai["unread_count"] == 1


@pytest.mark.asyncio
async def test_genre_counts_omit_empty_and_sort_desc(client: AsyncClient) -> None:
    await _seed_hierarchy()
    await _make_article("c1", "ai_llm")
    await _make_article("d1", "dev")
    await _make_article("d2", "dev")

    rows = (await client.get("/api/articles/genres")).json()
    assert [r["genre"] for r in rows] == ["dev", "ai"]
    assert all(r["unread_count"] > 0 for r in rows)
    assert next(r for r in rows if r["genre"] == "dev")["children"] == []


@pytest.mark.asyncio
async def test_genre_counts_keep_reserved_other_at_top_level(client: AsyncClient) -> None:
    await _seed_hierarchy()
    await _make_article("o1", "other")

    rows = (await client.get("/api/articles/genres")).json()
    other = next(r for r in rows if r["genre"] == "other")
    assert other["label_ja"] == "その他"
    assert other["children"] == []
    assert other["direct_count"] == 1
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_genre_hierarchy.py -k genre_counts -v`
Expected: FAIL。`KeyError: 'direct_count'`

- [ ] **Step 3: スキーマを階層にする**

`backend/app/schemas.py` の `GenreCountOut` を置き換える。

```python
class GenreCountOut(BaseModel):
    genre: str
    label_ja: str
    # direct_count + 子の合計。サイドバーの親行に出す数字
    unread_count: int
    # そのキーが直接付いている記事数（子ルールがまだ無いタグの記事）
    direct_count: int = 0
    children: list["GenreCountOut"] = []
```

- [ ] **Step 4: 集計を親子に畳む**

`backend/app/routers/articles.py` の `get_genre_counts` を置き換える。集計クエリ自体は変えない（3 列だけを読む軽い経路を維持する）。

```python
@router.get("/articles/genres", response_model=list[GenreCountOut])
async def get_genre_counts(session: AsyncSession = Depends(get_session)):
    """未読・未保存・未非表示の記事をジャンル別に数える。親は子の合計を含む。件数降順。"""
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
        )
    ).all()
    direct = {genre: cnt for genre, cnt in rows}

    genre_rows = (
        await session.execute(select(Genre.id, Genre.key, Genre.label_ja, Genre.parent_id))
    ).all()
    label_by_key = {key: label for _gid, key, label, _parent in genre_rows}
    key_by_id = {gid: key for gid, key, _label, _parent in genre_rows}
    children_keys: dict[str, list[str]] = {}
    for _gid, key, _label, parent_id in genre_rows:
        parent_key = key_by_id.get(parent_id) if parent_id is not None else None
        if parent_key:
            children_keys.setdefault(parent_key, []).append(key)

    def node(key: str) -> GenreCountOut:
        children = [node(c) for c in children_keys.get(key, [])]
        children = [c for c in children if c.unread_count > 0]
        children.sort(key=lambda c: (-c.unread_count, c.genre))
        own = direct.get(key, 0)
        return GenreCountOut(
            genre=key,
            label_ja=label_by_key.get(key, key),
            direct_count=own,
            unread_count=own + sum(c.unread_count for c in children),
            children=children,
        )

    # トップレベル = 親を持たないジャンル + 予約キー + 定義が消えた孤児キー
    top_keys = [key for _gid, key, _label, parent_id in genre_rows if parent_id is None]
    top_keys += [
        key for key in direct if key not in label_by_key and key != OTHER_GENRE
    ]
    if OTHER_GENRE in direct:
        top_keys.append(OTHER_GENRE)

    out = [node(key) for key in top_keys]
    out = [n for n in out if n.unread_count > 0]
    for n in out:
        if n.genre == OTHER_GENRE:
            n.label_ja = "その他"
    out.sort(key=lambda n: (-n.unread_count, n.genre))
    return out
```

`Genre` は `articles.py` のトップレベル import に既にあるか確認し、無ければ `from app.models import ..., Genre` に足す。

- [ ] **Step 5: テストが通ることを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_genre_hierarchy.py -v`
Expected: 全件 PASS

- [ ] **Step 6: 既存の件数テストが壊れていないことを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_genres_api.py -q`
Expected: `test_genre_counts_group_unread_unsaved_articles` と `test_genre_counts_label_for_reserved_other` が PASS

- [ ] **Step 7: コミット**

```bash
git add backend/app/schemas.py backend/app/routers/articles.py backend/tests/test_genre_hierarchy.py
git commit -m "feat: return genre counts as a parent/child tree"
```

---

### Task 6: ジャンル CRUD の親子対応

**Files:**
- Modify: `backend/app/schemas.py:149-170`（`GenreOut`, `GenreCreate`, `GenreUpdate`）
- Modify: `backend/app/routers/genres.py`
- Test: `backend/tests/test_genre_hierarchy.py`（追記）

**Interfaces:**
- Consumes: Task 1 の `Genre.parent_id`
- Produces: `GenreCreate.parent_id: int | None = None`、`GenreUpdate.parent_id: int | None = None`、`GenreOut.parent_id: int | None = None`

- [ ] **Step 1: 失敗するテストを書く**

```python
@pytest.mark.asyncio
async def test_create_child_genre(client: AsyncClient) -> None:
    genres = (await client.get("/api/genres")).json()
    parent_id = next(g["id"] for g in genres if g["key"] == "ai")

    resp = await client.post(
        "/api/genres",
        json={"key": "ai_llm", "label_ja": "LLM・生成AI", "priority": 1, "parent_id": parent_id},
    )
    assert resp.status_code == 201
    assert resp.json()["parent_id"] == parent_id


@pytest.mark.asyncio
async def test_cannot_nest_deeper_than_two_levels(client: AsyncClient) -> None:
    genres = (await client.get("/api/genres")).json()
    parent_id = next(g["id"] for g in genres if g["key"] == "ai")
    child = (
        await client.post(
            "/api/genres",
            json={"key": "ai_llm", "label_ja": "LLM", "priority": 1, "parent_id": parent_id},
        )
    ).json()

    resp = await client.post(
        "/api/genres",
        json={"key": "ai_llm_rag", "label_ja": "RAG", "priority": 1, "parent_id": child["id"]},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_cannot_set_parent_to_self_or_descendant(client: AsyncClient) -> None:
    genres = (await client.get("/api/genres")).json()
    parent_id = next(g["id"] for g in genres if g["key"] == "ai")
    child = (
        await client.post(
            "/api/genres",
            json={"key": "ai_llm", "label_ja": "LLM", "priority": 1, "parent_id": parent_id},
        )
    ).json()

    assert (
        await client.patch(f"/api/genres/{parent_id}", json={"parent_id": parent_id})
    ).status_code == 400
    assert (
        await client.patch(f"/api/genres/{parent_id}", json={"parent_id": child["id"]})
    ).status_code == 400


@pytest.mark.asyncio
async def test_promote_child_to_top_level(client: AsyncClient) -> None:
    genres = (await client.get("/api/genres")).json()
    parent_id = next(g["id"] for g in genres if g["key"] == "ai")
    child = (
        await client.post(
            "/api/genres",
            json={"key": "ai_llm", "label_ja": "LLM", "priority": 1, "parent_id": parent_id},
        )
    ).json()

    resp = await client.patch(f"/api/genres/{child['id']}", json={"parent_id": None})
    assert resp.status_code == 200
    assert resp.json()["parent_id"] is None


@pytest.mark.asyncio
async def test_create_child_404_for_missing_parent(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/genres",
        json={"key": "x", "label_ja": "X", "priority": 1, "parent_id": 99999},
    )
    assert resp.status_code == 404
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_genre_hierarchy.py -k "child or parent_to_self or two_levels" -v`
Expected: FAIL。`parent_id` がレスポンスに無い（`KeyError`）。

- [ ] **Step 3: スキーマに `parent_id` を足す**

```python
class GenreOut(BaseModel):
    id: int
    key: str
    label_ja: str
    priority: int
    # NULL が親ジャンル。値を持つものが子（階層は 2 段固定）
    parent_id: int | None = None
    # 管理 UI がチップの削除に rule id を使うので、タグ名だけでなく id も返す
    rules: list[GenreRuleOut] = []
    generic_rules: list[GenreRuleOut] = []
    # 変更系エンドポイントは全件再分類した件数をここに詰める（作成直後は 0）
    reclassified: int = 0


class GenreCreate(BaseModel):
    key: str
    label_ja: str
    priority: int = 100
    parent_id: int | None = None


class GenreUpdate(BaseModel):
    label_ja: str | None = None
    priority: int | None = None
    # 明示的に None を送ると親を外してトップレベルへ上げる。
    # 「未指定」と区別するため、ルーター側は model_fields_set を見る
    parent_id: int | None = None
```

- [ ] **Step 4: ルーターに検証を足す**

`backend/app/routers/genres.py` に共通の検証を追加する。

```python
async def _validate_parent(
    session: AsyncSession, parent_id: int | None, *, moving_id: int | None = None
) -> None:
    """親指定の妥当性を見る。階層は 2 段固定。"""
    if parent_id is None:
        return
    parent = await session.get(Genre, parent_id)
    if not parent:
        raise HTTPException(status_code=404, detail="Parent genre not found")
    if parent.parent_id is not None:
        raise HTTPException(status_code=400, detail="Genres can only nest one level deep")
    if moving_id is not None and parent_id == moving_id:
        raise HTTPException(status_code=400, detail="A genre cannot be its own parent")
    if moving_id is not None:
        # 自分の子を親にすると循環する
        child_ids = {
            gid
            for (gid,) in (
                await session.execute(select(Genre.id).where(Genre.parent_id == moving_id))
            ).all()
        }
        if parent_id in child_ids:
            raise HTTPException(status_code=400, detail="A genre cannot be its own parent")
```

`create_genre` は `Genre(...)` を作る前に `await _validate_parent(session, body.parent_id)` を呼び、`Genre(key=..., label_ja=..., priority=..., parent_id=body.parent_id)` にする。返す `GenreOut` に `parent_id=genre.parent_id` を足す。

`update_genre` は priority 更新の後に次を足す。

```python
    if "parent_id" in body.model_fields_set:
        await _validate_parent(session, body.parent_id, moving_id=genre_id)
        genre.parent_id = body.parent_id
```

`_list_genres` の `GenreOut(...)` に `parent_id=genre.parent_id` を足す。

- [ ] **Step 5: テストが通ることを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_genre_hierarchy.py -v`
Expected: 全件 PASS

- [ ] **Step 6: 全体テスト**

Run: `cd backend && .venv/bin/python -m pytest -q`
Expected: 全件 PASS

- [ ] **Step 7: コミット**

```bash
git add backend/app/schemas.py backend/app/routers/genres.py backend/tests/test_genre_hierarchy.py
git commit -m "feat: create and move genres between parent and child levels"
```

---

### Task 7: 推奨サブジャンルの冪等な投入

**Files:**
- Modify: `backend/app/services/genre_seed.py`
- Modify: `backend/app/routers/genres.py`
- Modify: `backend/app/schemas.py`（`SeedSubgenresResult` を追加）
- Test: `backend/tests/test_subgenre_seed.py`（新規）

**Interfaces:**
- Consumes: Task 6 の `_validate_parent`（は使わない。DB へ直接投入する）、Task 1 の `Genre.parent_id`
- Produces: `SUBGENRE_SEED: list[tuple[str, list[tuple[str, str, list[str]]]]]`（親 key → [(子 key, 子 label, タグ)]）、`async def seed_subgenres(session: AsyncSession) -> tuple[int, int]`（作成した子数, 付け替えたルール数）、`POST /genres/seed-subgenres` → `SeedSubgenresResult(created, moved, reclassified)`

- [ ] **Step 1: 失敗するテストを書く**

`backend/tests/test_subgenre_seed.py` を新規作成する（fixture は Task 1 と同じ形をコピーする）。

```python
"""推奨サブジャンルの投入テスト。

起動時に自動投入はしない（既存環境では約 15 秒ブロックし、利用者から見れば
「勝手に分類が変わった」になる）。明示操作のエンドポイントとして提供し、
何度押しても差分が出ないことと、利用者が動かしたタグを戻さないことを担保する。
"""

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
async def test_startup_does_not_create_subgenres(client: AsyncClient) -> None:
    """自動投入しないこと。押されるまで階層は増えない。"""
    genres = (await client.get("/api/genres")).json()
    assert all(g["parent_id"] is None for g in genres)


@pytest.mark.asyncio
async def test_seed_creates_children_and_moves_tags(client: AsyncClient) -> None:
    resp = await client.post("/api/genres/seed-subgenres")
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] == 8  # ai 3 + dev 5
    assert body["moved"] > 0

    genres = (await client.get("/api/genres")).json()
    by_key = {g["key"]: g for g in genres}
    ai_id = by_key["ai"]["id"]
    assert by_key["ai_llm"]["parent_id"] == ai_id
    assert by_key["ai_misc"]["parent_id"] == ai_id
    # 代表タグ ai は子へ降りて、親の直下ルールは空になる
    assert [r["tag"] for r in by_key["ai_misc"]["rules"]] == ["ai"]
    assert by_key["ai"]["rules"] == []
    assert by_key["ai"]["generic_rules"] == []
    # technology は汎用ルールのまま子へ移る
    assert [r["tag"] for r in by_key["dev_general"]["generic_rules"]] == ["technology"]
    assert [r["tag"] for r in by_key["dev_general"]["rules"]] == []


@pytest.mark.asyncio
async def test_seed_is_idempotent(client: AsyncClient) -> None:
    first = (await client.post("/api/genres/seed-subgenres")).json()
    second = (await client.post("/api/genres/seed-subgenres")).json()
    assert first["created"] == 8
    assert second == {"created": 0, "moved": 0, "reclassified": 0}


@pytest.mark.asyncio
async def test_seed_does_not_take_back_a_tag_the_user_moved(client: AsyncClient) -> None:
    """利用者が別ジャンルへ移したタグは、対象の親に属していないので触らない。"""
    genres = (await client.get("/api/genres")).json()
    security_id = next(g["id"] for g in genres if g["key"] == "security")
    await client.post(
        "/api/genre-rules", json={"tag": "llm", "genre_id": security_id, "is_generic": False}
    )

    await client.post("/api/genres/seed-subgenres")

    genres = (await client.get("/api/genres")).json()
    by_key = {g["key"]: g for g in genres}
    assert "llm" in [r["tag"] for r in by_key["security"]["rules"]]
    assert "llm" not in [r["tag"] for r in by_key["ai_llm"]["rules"]]


@pytest.mark.asyncio
async def test_seed_reclassifies_existing_articles(client: AsyncClient) -> None:
    import json

    from app.database import async_session
    from app.models import Article, Feed

    async with async_session() as session:
        feed = Feed(url="https://example.com/feed", title="Test Feed")
        session.add(feed)
        await session.flush()
        session.add(
            Article(
                feed_id=feed.id,
                guid="a1",
                url="https://example.com/a1",
                title="LLM の話",
                summary="",
                genre="ai",
                tag_suggestions=json.dumps(["llm", "ai"]),
            )
        )
        await session.commit()

    resp = await client.post("/api/genres/seed-subgenres")
    assert resp.json()["reclassified"] == 1

    rows = (await client.get("/api/articles/genres")).json()
    ai = next(r for r in rows if r["genre"] == "ai")
    assert ai["direct_count"] == 0
    assert [(c["genre"], c["unread_count"]) for c in ai["children"]] == [("ai_llm", 1)]


@pytest.mark.asyncio
async def test_specific_sibling_beats_the_catch_all_child(client: AsyncClient) -> None:
    """受け皿 (ai_misc) は具体的な兄弟 (ai_llm) に負けること。

    兄弟は親と同じ priority を持つので同順位になり、_resolve の同値解決は
    キーの辞書順で決まる。受け皿のキーが兄弟より前に来ると（例えば
    ai_general）、`llm` を持つ記事まで受け皿に吸われて分割の意味が薄れる。
    """
    import json

    from app.database import async_session
    from app.models import Article, Feed
    from app.services.genre_classifier import classify, load_rules, parse_tags

    async with async_session() as session:
        feed = Feed(url="https://example.com/feed2", title="Test Feed 2")
        session.add(feed)
        await session.flush()
        session.add(
            Article(
                feed_id=feed.id,
                guid="b1",
                url="https://example.com/b1",
                title="LLM と AI",
                summary="",
                tag_suggestions=json.dumps(["ai", "llm"]),
            )
        )
        await session.commit()

    await client.post("/api/genres/seed-subgenres")

    async with async_session() as session:
        rules = await load_rules(session)
        assert classify(parse_tags(json.dumps(["ai", "llm"])), rules) == "ai_llm"
        # 受け皿は具体的なタグが無いときだけ使われる
        assert classify(parse_tags(json.dumps(["ai"])), rules) == "ai_misc"
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_subgenre_seed.py -v`
Expected: FAIL。`test_startup_does_not_create_subgenres` のみ PASS、他は 404（エンドポイント未実装）。

- [ ] **Step 3: シード表と投入関数を書く**

`backend/app/services/genre_seed.py` の末尾に追加する。

```python
# 推奨サブジャンル: (親 key, [(子 key, 子 label_ja, タグ)])
# 実データのタグ分布（2026-08-17、未読 42/34 件）から作った。親の代表タグ
# （ai / technology など）も子へ降ろして親を純粋な入れ物にする——降ろさないと
# 最大の束が分割前とほぼ変わらない。
#
# 兄弟は親と同じ priority を持つので必ず同順位になり、_resolve の同値解決
# （キーの辞書順）で決まる。したがって「受け皿」の子は、具体的な兄弟より
# 後にソートされるキーを付けないと具体的な兄弟の記事を吸ってしまう。
# ai の受け皿は ai_misc（ai_infra < ai_llm < ai_misc）。dev の受け皿
# dev_general は technology が汎用ルールで、通常ルールの兄弟と同じ段で
# 競合しないため改名の必要がない。
SUBGENRE_SEED: list[tuple[str, list[tuple[str, str, list[str]]]]] = [
    ("ai", [
        ("ai_llm", "LLM・生成AI",
         ["llm", "openai", "claude", "chatgpt", "gemini", "genai", "rag", "mcp"]),
        ("ai_misc", "AI 全般", ["ai"]),
        ("ai_infra", "AI ハードウェア", ["nvidia"]),
    ]),
    ("dev", [
        ("dev_prog", "プログラミング",
         ["programming", "python", "rust", "javascript", "web", "api", "github",
          "vscode", "unity"]),
        ("dev_infra", "クラウド・インフラ",
         ["cloud", "aws", "linux", "windows", "microsoft", "network"]),
        ("dev_data", "データ・DB", ["database", "data", "excel"]),
        ("dev_tools", "ツール・ハード",
         ["tools", "software", "it", "performance", "hardware"]),
        ("dev_general", "技術一般", ["technology"]),
    ]),
]


async def seed_subgenres(session: AsyncSession) -> tuple[int, int]:
    """推奨サブジャンルを冪等に投入し、(作成した子数, 付け替えたルール数) を返す。

    - 既に存在する子キーには触らない
    - タグの付け替えは「現在その親ジャンルに属しているタグ」だけを対象にする。
      別ジャンルにあるものは利用者が移したか元から別扱いなので動かさない。
      この規則なら「利用者が動かしたのか、まだ投入していないのか」を区別する
      必要がない
    - is_generic は元のルールの値を保つ（technology は汎用のまま子へ移る）
    - commit と再分類は呼び出し側が行う
    """
    created = 0
    moved = 0
    for parent_key, children in SUBGENRE_SEED:
        parent = (
            await session.execute(select(Genre).where(Genre.key == parent_key))
        ).scalar_one_or_none()
        if parent is None or parent.parent_id is not None:
            continue  # 未定義の親、または既に子になっている親は対象外

        for child_key, child_label, tags in children:
            child = (
                await session.execute(select(Genre).where(Genre.key == child_key))
            ).scalar_one_or_none()
            if child is None:
                child = Genre(
                    key=child_key,
                    label_ja=child_label,
                    priority=parent.priority,
                    parent_id=parent.id,
                )
                session.add(child)
                await session.flush()
                created += 1

            for tag in tags:
                rule = (
                    await session.execute(select(GenreRule).where(GenreRule.tag == tag))
                ).scalar_one_or_none()
                if rule is None or rule.genre_id != parent.id:
                    continue
                rule.genre_id = child.id
                moved += 1
    await session.flush()
    return created, moved
```

子の `priority` を親と同じにするのが要点。親と同値なら「祖先・子孫の枝刈り」で子が勝ち、他ジャンルとの優劣は親のときと変わらない。

- [ ] **Step 4: スキーマとエンドポイントを足す**

`backend/app/schemas.py` に追加する。

```python
class SeedSubgenresResult(BaseModel):
    created: int
    moved: int
    reclassified: int
```

`backend/app/routers/genres.py` に追加する。

```python
@router.post("/genres/seed-subgenres", response_model=SeedSubgenresResult)
async def seed_recommended_subgenres(session: AsyncSession = Depends(get_session)):
    """推奨サブジャンルを投入する。

    起動時の自動投入はしない。既存環境では数千件の genre 付け替えで FTS の
    再インデックスが走り（実測 6,408 件で約 15 秒）、押していない利用者から
    見れば「勝手に分類が変わった」になるため。
    """
    from app.services.genre_seed import seed_subgenres

    created, moved = await seed_subgenres(session)
    await session.commit()
    changed = await _reclassify(session) if (created or moved) else 0
    return SeedSubgenresResult(created=created, moved=moved, reclassified=changed)
```

`SeedSubgenresResult` を import に足す。ルート順の注意: `POST /genres` と競合しないパス（`/genres/seed-subgenres`）なので既存定義の後に置いて問題ない。

- [ ] **Step 5: テストが通ることを確認**

Run: `cd backend && .venv/bin/python -m pytest tests/test_subgenre_seed.py -v`
Expected: 7 件 PASS

- [ ] **Step 6: 全体テスト**

Run: `cd backend && .venv/bin/python -m pytest -q`
Expected: 全件 PASS

- [ ] **Step 7: コミット**

```bash
git add backend/app/services/genre_seed.py backend/app/routers/genres.py backend/app/schemas.py backend/tests/test_subgenre_seed.py
git commit -m "feat: add idempotent recommended-subgenre seeding"
```

---

### Task 8: フロントの型・API クライアント・hooks

**Files:**
- Modify: `frontend/src/types/index.ts:34-53`（`GenreCount`, `GenreDef`）と `91-103`（`ArticleFilters`）
- Modify: `frontend/src/api/client.ts:39-68`（`getArticles`）、`134-148`（`createGenre`, `updateGenre`）
- Modify: `frontend/src/hooks/useGenres.ts`
- Test: 型チェックのみ（`npx tsc --noEmit`）

**Interfaces:**
- Consumes: Task 5 の `GenreCountOut`、Task 6 の `GenreOut.parent_id`、Task 7 の `SeedSubgenresResult`
- Produces: `GenreCount.direct_count: number`、`GenreCount.children: GenreCount[]`、`ArticleFilters.genre_exact?: boolean`、`GenreDef.parent_id: number | null`、`api.seedSubgenres()`、`useSeedSubgenres()`

- [ ] **Step 1: 型を更新**

`frontend/src/types/index.ts`

```typescript
export interface GenreCount {
  genre: string;
  label_ja: string;
  /** direct_count + 子の合計 */
  unread_count: number;
  /** そのキーが直接付いている記事数（子ルールがまだ無いタグの記事） */
  direct_count: number;
  children: GenreCount[];
}
```

`GenreDef` に `parent_id: number | null;` を足す。`ArticleFilters` に `genre_exact?: boolean;` を足す。

- [ ] **Step 2: API クライアントを更新**

`frontend/src/api/client.ts` の `getArticles` の genre 節に 1 行足す。

```typescript
  if (filters.genre) params.set('genre', filters.genre);
  if (filters.genre_exact) params.set('genre_exact', 'true');
```

`createGenre` / `updateGenre` の body 型に `parent_id` を足す。

```typescript
export function createGenre(body: { key: string; label_ja: string; priority: number; parent_id?: number | null }): Promise<GenreDef> {
  return fetchJSON(`${BASE}/genres`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function updateGenre(id: number, body: { label_ja?: string; priority?: number; parent_id?: number | null }): Promise<GenreDef> {
  return fetchJSON(`${BASE}/genres/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function seedSubgenres(): Promise<{ created: number; moved: number; reclassified: number }> {
  return fetchJSON(`${BASE}/genres/seed-subgenres`, { method: 'POST' });
}
```

- [ ] **Step 3: hooks を更新**

`frontend/src/hooks/useGenres.ts` の `useCreateGenre` / `useUpdateGenre` の引数型に `parent_id?: number | null` を足し、末尾に追加する。

```typescript
export function useSeedSubgenres() {
  const invalidate = useInvalidateGenreDefs();
  return useMutation({ mutationFn: api.seedSubgenres, onSuccess: invalidate });
}
```

- [ ] **Step 4: 型チェック**

Run: `cd frontend && npx tsc --noEmit`
Expected: FAIL。`FeedSidebar.tsx` が `GenreCount` の必須プロパティ `direct_count` / `children` を満たさない箇所は無いはずだが、`genreCounts.map` の使い方によってエラーが出る場合は Task 9 で解消する。ここでエラーが出るなら内容を記録して Task 9 へ進む。

- [ ] **Step 5: コミット**

```bash
git add frontend/src/types/index.ts frontend/src/api/client.ts frontend/src/hooks/useGenres.ts
git commit -m "feat: add subgenre fields to frontend types and API client"
```

---

### Task 9: サイドバーの階層表示

**Files:**
- Modify: `frontend/src/components/layout/FeedSidebar.tsx:293-317`（ジャンルナビ）
- Modify: `frontend/src/components/layout/FeedSidebar.tsx`（`genre: undefined` を書いている全ての箇所）
- Modify: `frontend/src/components/articles/ArticleList.tsx`（`filters.genre` を見て一括操作ボタンを出す箇所）
- Test: 手動確認（Playwright / ブラウザ）

**Interfaces:**
- Consumes: Task 8 の `GenreCount.children`、`ArticleFilters.genre_exact`
- Produces: `GENRE_SPLIT_THRESHOLD = 30`

- [ ] **Step 1: 閾値定数とジャンル行コンポーネントを書く**

`FeedSidebar.tsx` のファイル先頭付近（他の定数の隣）に置く。

```typescript
// 親の未読がこれを超えたらサイドバーで子ジャンルを展開する。
// 「これだけなら片付けられる」と思える大きさに束を割るための境界
const GENRE_SPLIT_THRESHOLD = 30;
```

ジャンルナビを次に置き換える。

```tsx
        {/* ジャンル別ナビゲーション。件数 0 のジャンルは API が返さないのでそのまま並べる。
            親の未読が閾値を超えたときだけ子を展開する（超えていなければ従来通り 1 行） */}
        {genreCounts && genreCounts.length > 0 && (
          <div className="mt-4">
            <div className="px-2 mb-1 text-xs font-semibold text-gray-400">ジャンル</div>
            {genreCounts.map((g) => {
              const expanded = g.children.length > 0 && g.unread_count > GENRE_SPLIT_THRESHOLD;
              return (
                <div key={g.genre}>
                  <GenreNavRow
                    label={g.label_ja}
                    count={g.unread_count}
                    active={filters.genre === g.genre && !filters.genre_exact}
                    // 子を展開している親行は集計の見出しなので、超過していても警告色にしない
                    warn={!expanded && g.unread_count > GENRE_SPLIT_THRESHOLD}
                    onClick={() => selectGenre(g.genre)}
                  />
                  {expanded && (
                    <>
                      {g.children.map((c) => (
                        <GenreNavRow
                          key={c.genre}
                          label={c.label_ja}
                          count={c.unread_count}
                          indent
                          active={filters.genre === c.genre}
                          warn={c.unread_count > GENRE_SPLIT_THRESHOLD}
                          onClick={() => selectGenre(c.genre)}
                        />
                      ))}
                      {g.direct_count > 0 && (
                        <GenreNavRow
                          label="その他"
                          count={g.direct_count}
                          indent
                          active={filters.genre === g.genre && !!filters.genre_exact}
                          warn={g.direct_count > GENRE_SPLIT_THRESHOLD}
                          onClick={() => selectGenre(g.genre, { exact: true })}
                        />
                      )}
                    </>
                  )}
                </div>
              );
            })}
          </div>
        )}
```

同ファイル内（コンポーネント関数の外）に行コンポーネントを置く。

```tsx
/** ジャンルナビの 1 行。選べる束が閾値を超えていたら件数バッジを警告色にする */
function GenreNavRow({
  label, count, active, warn, indent, onClick,
}: {
  label: string;
  count: number;
  active: boolean;
  warn: boolean;
  indent?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      title={warn ? '子ジャンルを追加すると更に分けられます' : undefined}
      className={`w-full flex items-center gap-2 py-1 text-sm text-left rounded hover:bg-gray-100 dark:hover:bg-gray-800 ${
        indent ? 'pl-6 pr-2' : 'px-2'
      } ${active ? 'bg-gray-200 dark:bg-gray-800 font-semibold' : ''}`}
    >
      <span className="truncate flex-1">{indent ? `↳ ${label}` : label}</span>
      <span
        className={`text-xs rounded-full px-1.5 py-0.5 min-w-[20px] text-center shrink-0 text-white ${
          warn ? 'bg-amber-500' : 'bg-blue-500'
        }`}
      >
        {count}
      </span>
    </button>
  );
}
```

- [ ] **Step 2: `selectGenre` ヘルパを足して他フラグのクリアを 1 箇所にする**

`FeedSidebar` コンポーネント内に置く。既存のジャンルボタンは `onFilterChange({...})` を直書きしていたが、`genre_exact` を足すと書き忘れの温床になるためまとめる。

```typescript
  // ビューは filters の排他フラグで表せているので、ジャンルを選ぶときは他を明示的に消す
  const selectGenre = (genre: string, opts?: { exact?: boolean }) => {
    onFilterChange({
      ...filters,
      genre,
      genre_exact: opts?.exact ? true : undefined,
      dismissed: undefined, feed_id: undefined, is_saved: undefined,
      tag_id: undefined, untagged: undefined,
      recommended: undefined, unrecommended: undefined, extract_failed: undefined,
    });
  };
```

- [ ] **Step 3: `genre: undefined` を書いている全箇所に `genre_exact: undefined` を足す**

`FeedSidebar.tsx` の以下の行（`genre: undefined` を含むオブジェクト）すべてに `genre_exact: undefined,` を追加する。対象は「All」ボタン(133 行付近)、「Saved」ボタン(178 行付近)、「非表示にした記事」ボタン(321 行付近)、フィード行(338 行付近)。

Run で漏れを確認: `cd frontend && grep -n "genre: undefined" src/components/layout/FeedSidebar.tsx`
すべての行の近くに `genre_exact: undefined` があること。

`GenreManagerModal` から `other` へ飛ぶ箇所（531 行付近 `onFilterChange({ ...filters, genre: 'other', dismissed: undefined })`）には `genre_exact: undefined` を足す。

- [ ] **Step 4: `ArticleList.tsx` の一括操作の確認件数を合わせる**

`ArticleList.tsx` は `genreCounts?.find(g => g.genre === filters.genre)` で確認ダイアログの件数を引いている。階層化で子も引けるようにし、`genre_exact` のときは `direct_count` を使う。

```typescript
  // 一括操作ボタンの確認件数・表示名は、現在のビューの total ではなく
  // 「そのジャンルの未読件数」（= mark-all-read / dismiss が実際に処理する件数）から取る。
  // 親を選んでいるときは子を含む合計、「その他」を選んでいるときは直下だけ。
  const flatGenreCounts = useMemo(
    () => (genreCounts ?? []).flatMap(g => [g, ...g.children]),
    [genreCounts],
  );
  const genreCountEntry = filters.genre
    ? flatGenreCounts.find(g => g.genre === filters.genre)
    : undefined;
  const genreLabel = genreCountEntry?.label_ja ?? filters.genre ?? '';
  const genreUnreadCount = filters.genre_exact
    ? genreCountEntry?.direct_count ?? 0
    : genreCountEntry?.unread_count ?? 0;
```

一括操作の `mutate` に `genre_exact` を渡す。

```typescript
                    markAllRead.mutate({ genre: filters.genre, genre_exact: filters.genre_exact });
```

```typescript
                    dismiss.mutate({ genre: filters.genre, genre_exact: filters.genre_exact }, {
                      onSuccess: (r) => setLastDismissed({ ids: r.ids, count: r.dismissed }),
                    });
```

`api.markAllRead` / `api.dismissArticles` の body 型に `genre_exact?: boolean` を足す（`client.ts`）。`useMarkAllRead` / `useDismiss` の引数型にも足す（`useArticles.ts`）。

- [ ] **Step 5: 型チェックとビルド**

Run: `cd frontend && npx tsc --noEmit && npm run build`
Expected: エラー無し

- [ ] **Step 6: 手動確認**

Run: `cd backend && .venv/bin/python -m pytest -q` （バックエンドが壊れていないこと）
Run: `make dev`

ブラウザ（幅 1280 と 390 の両方）で確認する。

1. 投入前: ジャンルナビが従来通り 1 段で、30 を超えるジャンルの件数バッジが amber になっている
2. ジャンル管理から「推奨サブジャンルを投入」（Task 10）を押す
3. AI・LLM と開発・技術の下に子が字下げ表示される。30 以下のジャンルは 1 行のまま
4. 子をクリックするとその子の記事だけが出る
5. 親をクリックすると子の記事も含めて出る
6. 「その他」行が出る場合、それをクリックすると親直下の記事だけが出る
7. 子を選んだ状態で「まとめて既読」の確認ダイアログの件数が、その子のバッジと一致する

- [ ] **Step 7: コミット**

```bash
git add frontend/src
git commit -m "feat: expand subgenres in the sidebar above 30 unread"
```

---

### Task 10: ジャンル管理 UI の親子対応と投入ボタン

**Files:**
- Modify: `frontend/src/components/layout/GenreManagerModal.tsx`
- Test: 手動確認

**Interfaces:**
- Consumes: Task 8 の `useSeedSubgenres`、`GenreDef.parent_id`

- [ ] **Step 1: 一覧を親子の入れ子で表示**

`genres` を親子に整理してから描画する。

```typescript
  // 親→子の入れ子で並べる。priority 昇順は既存のまま
  const tree = useMemo(() => {
    const list = genres ?? [];
    const parents = list.filter(g => g.parent_id == null);
    return parents.map(p => ({
      parent: p,
      children: list.filter(c => c.parent_id === p.id),
    }));
  }, [genres]);
```

既存の `genres.map(...)` を `tree.map(({ parent, children }) => ...)` に置き換え、子行は `pl-6` で字下げして描画する（親行の描画ロジックはそのまま流用し、子には priority の上下ボタンを出さない — 親と同じ priority を保つのが分割の前提だから）。

- [ ] **Step 2: 新規ジャンル作成に親の選択を足す**

`newGenreParentId` state と `<select>` を足す。

```tsx
        <select
          value={newGenreParentId ?? ''}
          onChange={(e) => setNewGenreParentId(e.target.value ? Number(e.target.value) : null)}
          className="px-1.5 py-1 text-xs border rounded dark:bg-gray-800 dark:border-gray-600"
        >
          <option value="">親ジャンルとして作成</option>
          {(genres ?? []).filter(g => g.parent_id == null).map(g => (
            <option key={g.id} value={g.id}>{g.label_ja} の子</option>
          ))}
        </select>
```

`createGenre.mutate` の body に `parent_id: newGenreParentId` を足す。子として作る場合の `priority` は親と同じ値にする。

```typescript
              const parent = (genres ?? []).find(g => g.id === newGenreParentId);
              createGenre.mutate(
                {
                  key,
                  label_ja: newGenreLabel.trim(),
                  priority: parent ? parent.priority : DEFAULT_NEW_GENRE_PRIORITY,
                  parent_id: newGenreParentId,
                },
                ...
```

- [ ] **Step 3: タグ移動先の選択肢に子を含める**

`newRuleGenreId` の `<select>`（既存）の option を、`tree` を使って親と子の両方（子は `↳` 付き）にする。

```tsx
          {tree.map(({ parent, children }) => (
            <optgroup key={parent.id} label={parent.label_ja}>
              <option value={parent.id}>{parent.label_ja}</option>
              {children.map(c => (
                <option key={c.id} value={c.id}>{`↳ ${c.label_ja}`}</option>
              ))}
            </optgroup>
          ))}
```

- [ ] **Step 4: 投入ボタンを足す**

```tsx
        <button
          onClick={() => {
            if (!confirm('推奨サブジャンルを投入します。既存記事の再分類に十数秒かかります。')) return;
            seedSubgenres.mutate(undefined, {
              onSuccess: (r) => setLastReclassified(r.reclassified),
            });
          }}
          disabled={seedSubgenres.isPending}
          className="text-xs text-blue-500 hover:text-blue-700 disabled:opacity-50"
        >
          {seedSubgenres.isPending ? '投入中...' : '推奨サブジャンルを投入'}
        </button>
```

`const seedSubgenres = useSeedSubgenres();` を足す。

- [ ] **Step 5: 型チェックとビルド**

Run: `cd frontend && npx tsc --noEmit && npm run build`
Expected: エラー無し

- [ ] **Step 6: 手動確認**

`make dev` で確認する。

1. ジャンル管理を開くと親子が入れ子で並ぶ
2. 「親ジャンルとして作成」を選んで作ると従来通りトップレベルに増える
3. 「AI・LLM の子」を選んで作ると子として増え、サイドバーの展開対象になる
4. タグ移動の選択肢に子が `↳` 付きで出て、選ぶとそのタグが子に移る
5. 「推奨サブジャンルを投入」を 2 回押しても 2 回目は何も変わらない（`reclassified` が 0）

- [ ] **Step 7: コミット**

```bash
git add frontend/src/components/layout/GenreManagerModal.tsx
git commit -m "feat: manage parent/child genres and seed subgenres from the UI"
```

---

### Task 11: ドキュメント・バージョン・リリース

**Files:**
- Modify: `README.md`, `README.ja.md`, `CLAUDE.md`
- Modify: `backend/pyproject.toml`, `frontend/package.json`, `backend/uv.lock`, `frontend/package-lock.json`

- [ ] **Step 1: README を両方更新**

`README.md` のジャンルトリアージの箇条書きの直後に足す。

```markdown
- Subgenres — a genre can hold one level of children. When a parent's unread count goes over 30, the sidebar expands its children so no single bucket looks too big to clear; below the threshold it stays a single row. A tag rule points at whichever level owns it, and resolution prefers the more specific one, so moving a genre's dominant tag down to a child actually splits the bucket. Selecting a parent covers its children too; the synthetic "その他" row selects only what is classified directly on the parent. A bucket still over 30 gets an amber badge, since a skewed tag distribution cannot always be split by meaning. `ジャンル管理` has a `推奨サブジャンルを投入` button that installs a measured default split for AI・LLM and 開発・技術 (idempotent; re-running it only adds what is missing)
```

`README.ja.md` の対応位置に日本語版を足す。

```markdown
- サブジャンル——ジャンルは 1 段だけ子を持てる。親の未読が 30 件を超えるとサイドバーが子を展開し、どの束も「片付けられる大きさ」に見えるようにする（閾値以下なら従来通り 1 行）。タグのルールは親でも子でも指せて、解決はより具体的な方を優先するので、ジャンルの代表タグを子に降ろせば実際に束が割れる。親を選ぶと子の記事も含まれ、「その他」行を選ぶと親に直接分類された記事だけになる。30 を超えた束は件数バッジが amber になる（タグ分布が偏っているジャンルは意味では割り切れないため）。「ジャンル管理」の「推奨サブジャンルを投入」で、AI・LLM と開発・技術に実データから作った既定の分割を入れられる（冪等。押し直すと足りない分だけ入る）
```

- [ ] **Step 2: `CLAUDE.md` のジャンル節を更新**

`## Architecture` の「Genre triage」節に、階層について 3 点追記する。

- `genres.parent_id` で 1 段の子を持てる。`Article.genre` は葉のキーを持ち、親の件数は集計時に子の合計として導出する（記事側のスキーマ変更なし）
- 解決順は「通常ルール → 汎用ルール」の各段で、祖先と子孫が両方当たったら子孫を採り、その後 `priority` で決める
- `genre` パラメータは親なら子孫を含むキー集合に展開される（`app/services/genre_scope.py`）。`genre_exact=true` で親直下だけに限定できる

`### Data model` の `Genre` の説明に `parent_id`（自己参照、1 段のみ）を足す。

- [ ] **Step 3: バージョンを上げて lockfile を作り直す**

feat なので minor 相当だが、1.0 未満は patch で運用する慣例に従う（`AGENTS.md`）。現在の版から patch を 1 つ上げる。

```bash
cd backend && uv lock
cd ../frontend && npm install --package-lock-only
```

- [ ] **Step 4: 全テストと型チェック**

Run: `cd backend && .venv/bin/python -m pytest -q`
Run: `cd frontend && npx tsc --noEmit && npm run lint`
Expected: 全て通る

- [ ] **Step 5: コミット**

```bash
git add README.md README.ja.md CLAUDE.md backend/pyproject.toml frontend/package.json backend/uv.lock frontend/package-lock.json
git commit -m "docs: document genre subdivision and bump version"
```

- [ ] **Step 6: PR を作って `--no-ff` でマージ**

```bash
git push -u origin feat/genre-subdivision
gh pr create --title "feat: split large genres into subgenres (vX.Y.Z)" --body "<Summary / Why / Test plan>"
```

マージ後にタグを打ち、push する。

```bash
git checkout main && git pull origin main
git merge --no-ff feat/genre-subdivision -m "Merge feat/genre-subdivision: split large genres into subgenres (vX.Y.Z)"
git tag -a vX.Y.Z -m "vX.Y.Z: split large genres into subgenres"
git push origin main && git push origin vX.Y.Z
```

- [ ] **Step 7: 本番反映と動作確認**

```bash
cd frontend && npm run build
launchctl kickstart -k "gui/$(id -u)/com.ccxa.snoreader"
```

新しい PID を確認し、`http://localhost:8000/api/articles/genres` が `children` を含む形で返ることを確認する。**投入ボタンを押す前に**、spec の「実装時は投入前に同じロールバック計測をやり直して見積もること」に従って所要時間を測る。

```bash
time sqlite3 data/snoreader.db "
BEGIN;
UPDATE articles SET genre = genre || '_probe' WHERE tag_suggestions IS NOT NULL AND genre IN ('ai','dev');
SELECT changes();
ROLLBACK;
"
```

その後、ブラウザのジャンル管理から「推奨サブジャンルを投入」を押し、サイドバーで子が展開されることを確認する。

---

## Self-Review

**Spec coverage:**

| spec の節 | 実装タスク |
|---|---|
| データモデル（`parent_id`、2 段固定、葉のキー、cascade） | Task 1, 6 |
| 分類器（子孫優先、両段で枝刈り） | Task 2 |
| 件数 API（階層、`direct_count`） | Task 5 |
| 記事一覧・一括操作（子孫展開、`genre_exact`） | Task 3, 4 |
| ジャンル定義の編集（`parent_id` の作成・付け替え・400 検証） | Task 6 |
| 推奨サブジャンルの投入（冪等、明示ボタン） | Task 7, 10 |
| サイドバー（閾値 30、その他行、警告色） | Task 9 |
| ジャンル管理 UI（入れ子表示、親選択、移動先に子） | Task 10 |
| 初期投入の内容（8 子、technology は generic 維持） | Task 7 |
| テスト（spec の一覧すべて） | Task 1-7 の各テスト |
| 影響とコスト（投入前の再計測） | Task 11 Step 7 |

**型の一貫性:** `genre_keys(session, genre, *, exact)` は Task 3 で定義し Task 4 で同じ名前・同じ引数で使う。`GenreCount.direct_count` / `children` は Task 5（バックエンド）と Task 8（フロント型）と Task 9（描画）で一致。`seed_subgenres` の戻り値 `(created, moved)` は Task 7 のルーターと一致。`GENRE_SPLIT_THRESHOLD` は Task 9 のみで定義・使用。

**検証済みの箇所:** Task 1 の自己参照リレーション（`children` / `parent` + `remote_side="Genre.id"` + `cascade="all, delete-orphan"`）は、in-memory SQLite に同じ定義を起こして「子が引けること」「親削除で子が消えること」を確認済み。
