# ジャンル自動分割 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 未読が 50 件を超えた葉ジャンルに対して分割案を自動生成し、ユーザーが 1 クリックで適用できるようにする。

**Architecture:** DB に触らない純関数のプランナ（`genre_split_planner.py`）が既存 `genre_classifier.classify` を使って候補ルールをシミュレートし、適用後の件数を実測した提案を作る。提案は新テーブル `genre_split_suggestions` に永続化し、フィード取得サイクルの末尾で更新。適用は既存ジャンル変更と同じ作法（1 トランザクション → `reclassify_all()` → commit）。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x async / SQLite / pytest + pytest-asyncio / React 19 + TanStack Query + Tailwind v4

**Spec:** `docs/superpowers/specs/2026-08-21-genre-auto-split-design.md`

## Global Constraints

- 階層は 2 段固定。孫ジャンルを作ってはならない（`routers/genres.py` の `_validate_parent` が HTTP 400 で拒否する）
- 分類ロジックは `app/services/genre_classifier.classify` のみ。プランナは独自の分類を書かない
- 提案の件数は**必ず `classify` によるシミュレーション実測値**。推測値を保存してはならない
- `reclassify_all()` は本番実測 47 秒。apply 以外の経路で呼ばない
- LLM は「ラベル命名」にのみ使う。分類には使わない。テストでは必ずモックする
- routers / services から `app.ai.*` と兄弟 `app.services.*` は**関数本体の中で import**（module top-level 禁止）。`app.models` / `app.database` / `app.schemas` は top-level で可
- コメントは日本語、識別子は英語
- `other` は `genres` テーブルに行を持たない予約キー（`OTHER_GENRE`）

## 定数（spec より）

| 定数 | 値 | 置き場所 |
|---|---|---|
| `genre_unread_limit` | 50 | `app/config.py` (`SNOREADER_GENRE_UNREAD_LIMIT`) |
| `_MIN_CHILD_ARTICLES` | 8 | `genre_split_planner.py` |
| `_MAX_NEW_CHILDREN` | 4 | `genre_split_planner.py` |
| `_BIN_FILL_RATIO` | 0.8 | `genre_split_planner.py` |

## File Structure

| ファイル | 責務 |
|---|---|
| `backend/app/services/genre_split_planner.py` （新規） | 純関数のプランナ。DB に触らない。`SplitProposal` / `ProposedChild` / `plan_splits()` |
| `backend/app/services/genre_split_store.py` （新規） | DB 側。未読集計 → プランナ呼び出し → 提案の upsert（`refresh_split_suggestions`）、適用（`apply_suggestion`）、無視（`dismiss_suggestion`） |
| `backend/app/models.py` （変更） | `GenreSplitSuggestion` モデル追加 |
| `backend/app/main.py` （変更） | 新テーブルの ALTER TABLE 不要（新規テーブルは `create_all` が作る）。変更なしの見込み。確認のみ |
| `backend/app/config.py` （変更） | `genre_unread_limit: int = 50` |
| `backend/app/schemas.py` （変更） | `SplitSuggestionOut` / `ProposedChildOut` / `ApplySuggestionBody` / `ApplySuggestionResult` |
| `backend/app/routers/genres.py` （変更） | 4 エンドポイント追加 |
| `backend/app/services/feed_fetcher.py` （変更） | `fetch_all_feeds()` 末尾に `refresh_split_suggestions()` |
| `backend/app/ai/genre_namer.py` （新規） | タグ集合 → 日本語ラベル（LLM 1 回、失敗時タグ名） |
| `frontend/src/types.ts` （変更） | `SplitSuggestion` / `ProposedChild` 型 |
| `frontend/src/api/client.ts` （変更） | 4 関数 |
| `frontend/src/hooks/useSplitSuggestions.ts` （新規） | 一覧 / apply / dismiss |
| `frontend/src/components/layout/SplitSuggestionPanel.tsx` （新規） | 提案 UI |
| `frontend/src/components/layout/GenreManagerModal.tsx` （変更） | パネルを差し込む |
| `frontend/src/components/layout/FeedSidebar.tsx` （変更） | ジャンル節にバッジ |

---

### Task 1: プランナのデータ構造と「超過なし」の基底ケース

**Files:**
- Create: `backend/app/services/genre_split_planner.py`
- Test: `backend/tests/test_genre_split_planner.py`

**Interfaces:**
- Consumes: `app.services.genre_classifier.GenreRules`（既存、`tag_to_genre` / `generic_to_genre` / `priority` / `parent` の 4 フィールドを持つ frozen dataclass）、`classify(tags: list[str], rules: GenreRules) -> str`
- Produces:
  - `ProposedChild(key: str, label_ja: str, tags: tuple[str, ...], estimated_unread: int)` — frozen dataclass
  - `SplitProposal(genre_key: str, strategy: str, before: int, projected_max: int, children: tuple[ProposedChild, ...], demote_tags: tuple[str, ...])` — frozen dataclass
  - `plan_splits(articles: list[tuple[int, list[str]]], rules: GenreRules, *, limit: int) -> list[SplitProposal]`
  - 定数 `_MIN_CHILD_ARTICLES = 8`, `_MAX_NEW_CHILDREN = 4`, `_BIN_FILL_RATIO = 0.8`

- [ ] **Step 1: Write the failing test**

`backend/tests/test_genre_split_planner.py` を新規作成:

```python
"""ジャンル分割プランナのテスト。DB も LLM も使わない純関数テスト。"""

from __future__ import annotations

from app.services.genre_classifier import GenreRules


def _rules(
    tag_to_genre: dict[str, str],
    *,
    generic: dict[str, str] | None = None,
    priority: dict[str, int] | None = None,
    parent: dict[str, str] | None = None,
) -> GenreRules:
    """テスト用の GenreRules を組む。priority 未指定のジャンルは 100 とする。"""
    keys = set(tag_to_genre.values()) | set((generic or {}).values())
    keys |= set((parent or {}).keys()) | set((parent or {}).values())
    prio = {k: 100 for k in keys}
    prio.update(priority or {})
    return GenreRules(dict(tag_to_genre), dict(generic or {}), prio, dict(parent or {}))


def test_no_proposal_when_every_genre_is_under_the_limit() -> None:
    """上限以下しかないときは提案を出さない。"""
    from app.services.genre_split_planner import plan_splits

    rules = _rules({"python": "dev", "soccer": "sports"})
    articles = [(i, ["python"]) for i in range(5)] + [(100 + i, ["soccer"]) for i in range(5)]

    assert plan_splits(articles, rules, limit=50) == []
```

- [ ] **Step 2: Run test to verify it fails**

```fish
cd backend && .venv/bin/python -m pytest tests/test_genre_split_planner.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.genre_split_planner'`

- [ ] **Step 3: Write minimal implementation**

`backend/app/services/genre_split_planner.py`:

```python
"""未読が上限を超えた葉ジャンルの分割案を作る。

DB には触らない純関数。分類は既存 genre_classifier.classify を使い、
候補ルールに対して未読記事全件を実際に分類し直してから件数を出す
（推測値は出さない）。兄弟ジャンルは親と同じ priority を持つため
同順位になり、_resolve の同値解決（キーの辞書順）で決まる——
受け皿より後にソートされるキーの新兄弟は記事を 1 件も取れない。
シミュレーションを通せばこの失敗は projected 件数 0 として自動検出される。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace

from app.services.genre_classifier import OTHER_GENRE, GenreRules, classify

# 下限なしだと 2 件のジャンルが量産される（実データで未ルール共起タグは waymo 2 / google 2 級）
_MIN_CHILD_ARTICLES = 8
# 1 提案で辞書が大きく動きすぎないようにする
_MAX_NEW_CHILDREN = 4
# 貪欲詰めの 1 ビンあたり上限（limit のこの割合まで）。分割直後に再超過しないための余裕
_BIN_FILL_RATIO = 0.8


@dataclass(frozen=True)
class ProposedChild:
    key: str
    label_ja: str
    tags: tuple[str, ...]
    estimated_unread: int


@dataclass(frozen=True)
class SplitProposal:
    genre_key: str
    strategy: str
    before: int
    projected_max: int
    children: tuple[ProposedChild, ...]
    demote_tags: tuple[str, ...] = ()


def plan_splits(
    articles: list[tuple[int, list[str]]],
    rules: GenreRules,
    *,
    limit: int,
) -> list[SplitProposal]:
    """上限を超えた葉ジャンルごとに、成立した分割案を全部返す。

    articles は (article_id, tags) の列。rules のスナップショットで分類した
    結果が上限を超えたジャンルだけを対象にする。
    """
    return []
```

- [ ] **Step 4: Run test to verify it passes**

```fish
cd backend && .venv/bin/python -m pytest tests/test_genre_split_planner.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```fish
git add backend/app/services/genre_split_planner.py backend/tests/test_genre_split_planner.py
git commit -m "feat: add genre split planner skeleton"
```

---

### Task 2: 戦略 A — 自分の担当タグを兄弟に分ける

**Files:**
- Modify: `backend/app/services/genre_split_planner.py`
- Test: `backend/tests/test_genre_split_planner.py`

**Interfaces:**
- Consumes: Task 1 の `ProposedChild` / `SplitProposal` / `plan_splits` / 定数
- Produces:
  - `_leaf_keys(rules: GenreRules) -> set[str]` — 子を持たないジャンルキー集合（`parent` の値に現れないキー）
  - `_current_counts(articles, rules) -> Counter[str]`
  - `_simulate(articles, rules, *, tag_moves: dict[str, str], demote: set[str]) -> Counter[str]` — 候補ルールでの分類結果。`tag_moves` は tag→新ジャンルキー、`demote` は `is_generic` へ落とすタグ
  - `_plan_split_own_tags(genre_key, articles, rules, *, limit) -> SplitProposal | None` — strategy `"split_own_tags"`

- [ ] **Step 1: Write the failing test**

`backend/tests/test_genre_split_planner.py` に追記:

```python
def test_split_own_tags_puts_every_bin_under_the_limit() -> None:
    """担当タグが複数ある葉が超過したら、タグを兄弟に分けて全ビンを上限未満にする。"""
    from app.services.genre_split_planner import plan_splits

    # dev_prog が python 30 / rust 30 / api 30 = 90 件。上限 50 を超える
    rules = _rules(
        {"python": "dev_prog", "rust": "dev_prog", "api": "dev_prog"},
        priority={"dev": 3, "dev_prog": 3},
        parent={"dev_prog": "dev"},
    )
    articles = (
        [(i, ["python"]) for i in range(30)]
        + [(100 + i, ["rust"]) for i in range(30)]
        + [(200 + i, ["api"]) for i in range(30)]
    )

    proposals = [p for p in plan_splits(articles, rules, limit=50) if p.strategy == "split_own_tags"]
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.genre_key == "dev_prog"
    assert proposal.before == 90
    assert proposal.projected_max <= 50
    # 新しい兄弟が実際に記事を引き取っている（キーの辞書順で負けていない）
    assert proposal.children
    assert all(c.estimated_unread > 0 for c in proposal.children)
    # 提案されたタグの合計が元の担当タグの部分集合になっている
    proposed_tags = {t for c in proposal.children for t in c.tags}
    assert proposed_tags <= {"python", "rust", "api"}


def test_split_own_tags_children_are_siblings_under_the_same_parent() -> None:
    """新しい子のキーは親キーの接頭辞を持ち、受け皿より辞書順で前になる。"""
    from app.services.genre_split_planner import plan_splits

    rules = _rules(
        {"python": "dev_prog", "rust": "dev_prog", "api": "dev_prog"},
        priority={"dev": 3, "dev_prog": 3},
        parent={"dev_prog": "dev"},
    )
    articles = (
        [(i, ["python"]) for i in range(30)]
        + [(100 + i, ["rust"]) for i in range(30)]
        + [(200 + i, ["api"]) for i in range(30)]
    )

    proposal = next(
        p for p in plan_splits(articles, rules, limit=50) if p.strategy == "split_own_tags"
    )
    for child in proposal.children:
        assert child.key.startswith("dev_")
        assert child.key != "dev_prog"
```

- [ ] **Step 2: Run test to verify it fails**

```fish
cd backend && .venv/bin/python -m pytest tests/test_genre_split_planner.py -v
```

Expected: FAIL — `StopIteration` / `assert len(proposals) == 1` が 0 で落ちる（`plan_splits` がまだ `[]` を返す）

- [ ] **Step 3: Write minimal implementation**

`genre_split_planner.py` に追記し、`plan_splits` を実装:

```python
def _leaf_keys(rules: GenreRules) -> set[str]:
    """子を持たないジャンルキー。parent の値に現れるキーは親なので除く。"""
    all_keys = set(rules.priority)
    parents = set(rules.parent.values())
    return all_keys - parents


def _current_counts(articles: list[tuple[int, list[str]]], rules: GenreRules) -> Counter[str]:
    return Counter(classify(tags, rules) for _aid, tags in articles)


def _simulate(
    articles: list[tuple[int, list[str]]],
    rules: GenreRules,
    *,
    tag_moves: dict[str, str],
    demote: set[str],
    new_priorities: dict[str, int] | None = None,
) -> Counter[str]:
    """候補ルールで実際に分類し直した件数を返す。推測しない。"""
    tag_to_genre = dict(rules.tag_to_genre)
    generic_to_genre = dict(rules.generic_to_genre)
    for tag, genre in tag_moves.items():
        tag_to_genre[tag] = genre
    for tag in demote:
        if tag in tag_to_genre:
            generic_to_genre[tag] = tag_to_genre.pop(tag)
    priority = dict(rules.priority)
    priority.update(new_priorities or {})
    candidate = replace(
        rules,
        tag_to_genre=tag_to_genre,
        generic_to_genre=generic_to_genre,
        priority=priority,
    )
    return Counter(classify(tags, candidate) for _aid, tags in articles)


def _own_tags(genre_key: str, rules: GenreRules) -> list[str]:
    return [t for t, g in rules.tag_to_genre.items() if g == genre_key]


def _sibling_key(parent_key: str, tag: str) -> str:
    """新しい兄弟のキー。親キーの接頭辞を保ち、タグ名の非英数字を _ にする。"""
    slug = "".join(ch if ch.isalnum() else "_" for ch in tag.lower())
    return f"{parent_key}_{slug}"


def _plan_split_own_tags(
    genre_key: str,
    articles: list[tuple[int, list[str]]],
    rules: GenreRules,
    *,
    limit: int,
) -> SplitProposal | None:
    """担当タグを件数降順に貪欲に詰め、最多タグは元ジャンルに残す。"""
    before = _current_counts(articles, rules)[genre_key]
    own = _own_tags(genre_key, rules)
    if len(own) < 2:
        return None

    # このジャンルに落ちている記事だけを見てタグ件数を数える
    mine = [(aid, tags) for aid, tags in articles if classify(tags, rules) == genre_key]
    tag_counts = Counter(t for _aid, tags in mine for t in tags if t in own)
    ranked = [t for t, _c in tag_counts.most_common() if tag_counts[t] > 0]
    if len(ranked) < 2:
        return None

    # 最多タグ（受け皿）は元ジャンルに残す。残りを貪欲にビンへ詰める
    keep, movable = ranked[0], ranked[1:]
    bin_cap = max(1, int(limit * _BIN_FILL_RATIO))
    parent_key = rules.parent.get(genre_key, genre_key)

    bins: list[list[str]] = []
    for tag in movable:
        for b in bins:
            if sum(tag_counts[t] for t in b) + tag_counts[tag] <= bin_cap:
                b.append(tag)
                break
        else:
            if len(bins) >= _MAX_NEW_CHILDREN:
                break
            bins.append([tag])
    if not bins:
        return None

    tag_moves: dict[str, str] = {}
    keys: list[str] = []
    for b in bins:
        key = _sibling_key(parent_key, b[0])
        if key == genre_key:
            return None  # 受け皿と衝突するキーは作れない
        keys.append(key)
        for tag in b:
            tag_moves[tag] = key

    projected = _simulate(
        articles,
        rules,
        tag_moves=tag_moves,
        demote=set(),
        new_priorities={k: rules.priority.get(parent_key, 100) for k in keys},
    )
    children = tuple(
        ProposedChild(key=key, label_ja=b[0], tags=tuple(b), estimated_unread=projected[key])
        for key, b in zip(keys, bins)
    )
    # 1 件も引き取れない子が混ざる案は、キーの辞書順で負けている。棄却する
    if any(c.estimated_unread == 0 for c in children):
        return None
    if projected[genre_key] > limit:
        return None
    assert keep  # 受け皿タグは移さない
    return SplitProposal(
        genre_key=genre_key,
        strategy="split_own_tags",
        before=before,
        projected_max=max(projected.values()),
        children=children,
    )


def plan_splits(
    articles: list[tuple[int, list[str]]],
    rules: GenreRules,
    *,
    limit: int,
) -> list[SplitProposal]:
    """上限を超えた葉ジャンルごとに、成立した分割案を全部返す。

    articles は (article_id, tags) の列。rules のスナップショットで分類した
    結果が上限を超えたジャンルだけを対象にする。案は projected_max 昇順。
    """
    counts = _current_counts(articles, rules)
    leaves = _leaf_keys(rules) | {OTHER_GENRE}
    over = sorted(k for k, c in counts.items() if c > limit and k in leaves)

    proposals: list[SplitProposal] = []
    for genre_key in over:
        found = _plan_split_own_tags(genre_key, articles, rules, limit=limit)
        if found:
            proposals.append(found)
    proposals.sort(key=lambda p: (p.projected_max, p.genre_key, p.strategy))
    return proposals
```

- [ ] **Step 4: Run test to verify it passes**

```fish
cd backend && .venv/bin/python -m pytest tests/test_genre_split_planner.py -v
```

Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```fish
git add backend/app/services/genre_split_planner.py backend/tests/test_genre_split_planner.py
git commit -m "feat: plan splits by dividing a genre's own tags"
```

---

### Task 3: 戦略 C — 受け皿タグを汎用ルールに降格

**Files:**
- Modify: `backend/app/services/genre_split_planner.py`
- Test: `backend/tests/test_genre_split_planner.py`

**Interfaces:**
- Consumes: Task 2 の `_simulate` / `_current_counts` / `_own_tags` / `_leaf_keys`
- Produces: `_plan_demote_generic(genre_key, articles, rules, *, limit) -> SplitProposal | None` — strategy `"demote_generic"`、`children=()`、`demote_tags` に降格するタグ

`_DEMOTE_COVERAGE` = 0.8（そのジャンルの未読の 80% 以上に出るタグを受け皿とみなす）を追加。

- [ ] **Step 1: Write the failing test**

```python
def test_demote_generic_shrinks_a_receptacle_genre_without_adding_children() -> None:
    """担当タグが 1 つの受け皿が超過したら、そのタグを汎用に降格して他ジャンルに譲る。

    実データの ai_misc（担当タグは ai だけ、53 件）を縮めた再現。ai の priority が
    最小なので、ai + security の記事も ai_misc に落ちてしまっている。
    """
    from app.services.genre_split_planner import plan_splits

    rules = _rules(
        {"ai": "ai_misc", "llm": "ai_llm", "security": "security", "python": "dev"},
        priority={"ai": 1, "ai_misc": 1, "ai_llm": 1, "security": 2, "dev": 3},
        parent={"ai_misc": "ai", "ai_llm": "ai"},
    )
    # ai だけの記事 20 件 + ai と他ジャンルタグを併せ持つ記事 40 件 = ai_misc に 60 件
    articles = (
        [(i, ["ai"]) for i in range(20)]
        + [(100 + i, ["ai", "security"]) for i in range(20)]
        + [(200 + i, ["ai", "python"]) for i in range(20)]
    )

    proposals = [p for p in plan_splits(articles, rules, limit=50) if p.strategy == "demote_generic"]
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.genre_key == "ai_misc"
    assert proposal.before == 60
    assert proposal.demote_tags == ("ai",)
    assert proposal.children == ()          # ジャンルを増やさない手
    assert proposal.projected_max <= 50
```

- [ ] **Step 2: Run test to verify it fails**

```fish
cd backend && .venv/bin/python -m pytest tests/test_genre_split_planner.py::test_demote_generic_shrinks_a_receptacle_genre_without_adding_children -v
```

Expected: FAIL — `assert len(proposals) == 1` が 0 で落ちる

- [ ] **Step 3: Write minimal implementation**

定数を追加:

```python
# そのジャンルの未読の何割以上に出現するタグを「受け皿」とみなすか
_DEMOTE_COVERAGE = 0.8
```

関数を追加:

```python
def _plan_demote_generic(
    genre_key: str,
    articles: list[tuple[int, list[str]]],
    rules: GenreRules,
    *,
    limit: int,
) -> SplitProposal | None:
    """受け皿タグを is_generic に降格し、通常ルールを持つ他ジャンルに譲る。

    ジャンルを 1 つも増やさずに済むので最優先で試す。ただし
    「AI＋セキュリティの記事は security へ行く」という意味の変更を伴うため、
    採用するかどうかはユーザーが決める（案として並べるだけ）。
    """
    before = _current_counts(articles, rules)[genre_key]
    mine = [(aid, tags) for aid, tags in articles if classify(tags, rules) == genre_key]
    if not mine:
        return None
    own = set(_own_tags(genre_key, rules))
    if not own:
        return None

    threshold = len(mine) * _DEMOTE_COVERAGE
    counts = Counter(t for _aid, tags in mine for t in tags if t in own)
    receptacle = tuple(sorted(t for t, c in counts.items() if c >= threshold))
    if not receptacle:
        return None

    projected = _simulate(articles, rules, tag_moves={}, demote=set(receptacle))
    if projected[genre_key] > limit or max(projected.values()) > limit:
        return None
    return SplitProposal(
        genre_key=genre_key,
        strategy="demote_generic",
        before=before,
        projected_max=max(projected.values()),
        children=(),
        demote_tags=receptacle,
    )
```

`plan_splits` のループを差し替え:

```python
    proposals: list[SplitProposal] = []
    for genre_key in over:
        for planner in (_plan_demote_generic, _plan_split_own_tags):
            found = planner(genre_key, articles, rules, limit=limit)
            if found:
                proposals.append(found)
```

- [ ] **Step 4: Run test to verify it passes**

```fish
cd backend && .venv/bin/python -m pytest tests/test_genre_split_planner.py -v
```

Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```fish
git add backend/app/services/genre_split_planner.py backend/tests/test_genre_split_planner.py
git commit -m "feat: plan splits by demoting a receptacle tag to generic"
```

---

### Task 4: 戦略 B — 未ルール共起タグを兄弟に昇格（`other` を含む）

**Files:**
- Modify: `backend/app/services/genre_split_planner.py`
- Test: `backend/tests/test_genre_split_planner.py`

**Interfaces:**
- Consumes: Task 3 までの全ヘルパ
- Produces: `_plan_promote_free_tags(genre_key, articles, rules, *, limit) -> SplitProposal | None` — strategy `"promote_free_tags"`。`genre_key == OTHER_GENRE` のときは新しいトップレベルジャンル（`parent_key` を使わず、キーは `_sibling_key("genre", tag)` ではなくタグの slug そのもの）

- [ ] **Step 1: Write the failing test**

```python
def test_promote_free_tags_creates_siblings_from_unruled_cooccurring_tags() -> None:
    """未ルールの共起タグを新しい兄弟の担当タグにする。"""
    from app.services.genre_split_planner import plan_splits

    rules = _rules(
        {"ai": "ai_misc"},
        priority={"ai": 1, "ai_misc": 1},
        parent={"ai_misc": "ai"},
    )
    # agent 20 件 / benchmark 15 件 は未ルール。残り 25 件は ai だけ
    articles = (
        [(i, ["ai", "agent"]) for i in range(20)]
        + [(100 + i, ["ai", "benchmark"]) for i in range(15)]
        + [(200 + i, ["ai"]) for i in range(25)]
    )

    proposals = [
        p for p in plan_splits(articles, rules, limit=50) if p.strategy == "promote_free_tags"
    ]
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.before == 60
    assert proposal.projected_max <= 50
    assert {t for c in proposal.children for t in c.tags} == {"agent", "benchmark"}
    assert all(c.key.startswith("ai_") for c in proposal.children)


def test_promote_free_tags_ignores_tags_below_the_minimum_article_count() -> None:
    """2 件級の未ルールタグでジャンルを作らない（下限 _MIN_CHILD_ARTICLES）。

    実データの ai_misc の未ルール共起タグは waymo 2 / google 2 しかなく、
    下限がないと 2 件のジャンルが量産される。
    """
    from app.services.genre_split_planner import _MIN_CHILD_ARTICLES, plan_splits

    assert _MIN_CHILD_ARTICLES > 2
    rules = _rules({"ai": "ai_misc"}, priority={"ai": 1, "ai_misc": 1}, parent={"ai_misc": "ai"})
    articles = (
        [(i, ["ai", "waymo"]) for i in range(2)]
        + [(100 + i, ["ai", "google"]) for i in range(2)]
        + [(200 + i, ["ai"]) for i in range(60)]
    )

    assert [p for p in plan_splits(articles, rules, limit=50) if p.strategy == "promote_free_tags"] == []


def test_promote_free_tags_on_other_creates_top_level_genres() -> None:
    """other は genres に行がなくぶら下げ先がないので、新しいトップレベルを提案する。"""
    from app.services.genre_split_planner import plan_splits

    rules = _rules({"python": "dev"}, priority={"dev": 3})
    # どのルールにも当たらない記事 60 件。football 30 / drone 30
    articles = (
        [(i, ["football"]) for i in range(30)]
        + [(100 + i, ["drone"]) for i in range(30)]
    )

    proposals = [
        p for p in plan_splits(articles, rules, limit=50) if p.strategy == "promote_free_tags"
    ]
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.genre_key == "other"
    assert {c.key for c in proposal.children} == {"football", "drone"}
    assert proposal.projected_max <= 50
```

- [ ] **Step 2: Run test to verify it fails**

```fish
cd backend && .venv/bin/python -m pytest tests/test_genre_split_planner.py -v
```

Expected: FAIL — 3 つの新テストが `assert len(proposals) == 1` を 0 で落とす（`test_..._below_the_minimum...` だけは偶然通る可能性があるが、他 2 つが落ちる）

- [ ] **Step 3: Write minimal implementation**

```python
def _plan_promote_free_tags(
    genre_key: str,
    articles: list[tuple[int, list[str]]],
    rules: GenreRules,
    *,
    limit: int,
) -> SplitProposal | None:
    """そのジャンルの記事に多く共起する未ルールタグを、新しいジャンルの担当にする。

    other は genres に行を持たない予約キーなので、新しい兄弟ではなく
    新しいトップレベルジャンルを提案する（階層は 2 段のまま）。
    """
    before = _current_counts(articles, rules)[genre_key]
    mine = [(aid, tags) for aid, tags in articles if classify(tags, rules) == genre_key]
    if not mine:
        return None

    ruled = set(rules.tag_to_genre) | set(rules.generic_to_genre)
    counts = Counter(t for _aid, tags in mine for t in tags if t not in ruled)
    candidates = [(t, c) for t, c in counts.most_common() if c >= _MIN_CHILD_ARTICLES]
    candidates = candidates[:_MAX_NEW_CHILDREN]
    if not candidates:
        return None

    is_other = genre_key == OTHER_GENRE
    parent_key = genre_key if is_other else rules.parent.get(genre_key, genre_key)
    tag_moves: dict[str, str] = {}
    keys: list[str] = []
    for tag, _c in candidates:
        key = tag if is_other else _sibling_key(parent_key, tag)
        if key == genre_key or key in rules.priority:
            return None  # 既存キーと衝突する案は作らない
        keys.append(key)
        tag_moves[tag] = key

    # other 由来の新トップレベルは priority を既定の 100 とし、既存ジャンルを侵さない。
    # 兄弟のときは親と同じ priority（既存のサブジャンルと同じ作法）
    new_priority = 100 if is_other else rules.priority.get(parent_key, 100)
    projected = _simulate(
        articles,
        rules,
        tag_moves=tag_moves,
        demote=set(),
        new_priorities={k: new_priority for k in keys},
    )
    children = tuple(
        ProposedChild(key=key, label_ja=tag, tags=(tag,), estimated_unread=projected[key])
        for key, (tag, _c) in zip(keys, candidates)
    )
    if any(c.estimated_unread == 0 for c in children):
        return None  # キーの辞書順で負けている案
    if projected[genre_key] > limit:
        return None
    return SplitProposal(
        genre_key=genre_key,
        strategy="promote_free_tags",
        before=before,
        projected_max=max(projected.values()),
        children=children,
    )
```

`plan_splits` の planner タプルに追加:

```python
        for planner in (_plan_demote_generic, _plan_split_own_tags, _plan_promote_free_tags):
```

- [ ] **Step 4: Run test to verify it passes**

```fish
cd backend && .venv/bin/python -m pytest tests/test_genre_split_planner.py -v
```

Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```fish
git add backend/app/services/genre_split_planner.py backend/tests/test_genre_split_planner.py
git commit -m "feat: plan splits by promoting unruled co-occurring tags"
```

---

### Task 5: キーの辞書順で負ける案の棄却を明示的に固定する

**Files:**
- Test: `backend/tests/test_genre_split_planner.py`

**Interfaces:**
- Consumes: Task 4 までの `plan_splits`
- Produces: なし（回帰テストのみ）

このテストは spec の中核な不変条件を固定する。実装は Task 2/4 の `estimated_unread == 0` 棄却で既に通るはずだが、**この振る舞いが将来消えないように明示的に固定する**（既存 `genre_seed.py` のコメントが警告している罠そのもの）。

- [ ] **Step 1: Write the failing test**

```python
def test_a_sibling_key_sorting_after_the_receptacle_is_rejected() -> None:
    """受け皿より辞書順で後になる兄弟キーの案は棄却される。

    兄弟は親と同じ priority を持つので必ず同順位になり、_resolve の同値解決
    （キーの辞書順）で決まる。受け皿より後にソートされるキーの新兄弟は
    記事を 1 件も取れない。シミュレーションがこれを projected 0 で検出する。
    """
    from app.services.genre_split_planner import plan_splits

    # 受け皿は ai_aaa（辞書順で最初）。新兄弟 ai_zzz は必ず負ける
    rules = _rules({"ai": "ai_aaa"}, priority={"ai": 1, "ai_aaa": 1}, parent={"ai_aaa": "ai"})
    articles = (
        [(i, ["ai", "zzz"]) for i in range(30)]
        + [(100 + i, ["ai"]) for i in range(30)]
    )

    for proposal in plan_splits(articles, rules, limit=50):
        # 提案されたどの子も、必ず 1 件以上引き取れている
        assert all(c.estimated_unread > 0 for c in proposal.children)
```

- [ ] **Step 2: Run test to verify it fails or passes**

```fish
cd backend && .venv/bin/python -m pytest tests/test_genre_split_planner.py::test_a_sibling_key_sorting_after_the_receptacle_is_rejected -v
```

Expected: PASS（Task 2/4 の棄却ロジックで既に守られている）。**もし FAIL したら** `_plan_promote_free_tags` / `_plan_split_own_tags` の `estimated_unread == 0` 棄却が効いていないので、そこを直す。

- [ ] **Step 3: 実装（Step 2 が PASS なら不要）**

FAIL した場合のみ、該当プランナの棄却条件を見直す。棄却は「候補を捨てる」であって例外を投げない。

- [ ] **Step 4: 全プランナテストを走らせる**

```fish
cd backend && .venv/bin/python -m pytest tests/test_genre_split_planner.py -v
```

Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```fish
git add backend/tests/test_genre_split_planner.py
git commit -m "test: pin rejection of sibling keys that lose on key order"
```

---

### Task 6: LLM によるラベル命名

**Files:**
- Create: `backend/app/ai/genre_namer.py`
- Test: `backend/tests/test_genre_namer.py`

**Interfaces:**
- Consumes: `app.ai.llm_client.chat_completion(messages, *, max_tokens=512, temperature=0.3, priority=None, lane="reserved", frequency_penalty=None) -> str | None`、`app.ai.task_queue.PRIORITY_FOREGROUND`
- Produces: `async def name_genres(tag_groups: list[tuple[str, ...]]) -> list[str]` — 各タグ集合に対する日本語ラベル。LLM 失敗時は各グループの先頭タグをそのまま返す。**戻り値の長さは必ず入力と同じ**

- [ ] **Step 1: Write the failing test**

`backend/tests/test_genre_namer.py`:

```python
"""ジャンルのラベル命名のテスト。LLM は必ずモックする。"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_name_genres_parses_one_label_per_line(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ai import genre_namer

    async def fake_chat(messages, **kwargs):
        return "エージェント\nベンチマーク"

    monkeypatch.setattr(genre_namer, "chat_completion", fake_chat)

    labels = await genre_namer.name_genres([("agent",), ("benchmark", "eval")])
    assert labels == ["エージェント", "ベンチマーク"]


@pytest.mark.asyncio
async def test_name_genres_falls_back_to_the_first_tag_when_llm_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM が落ちても提案は作れなければならない（ラベルは後から編集できる）。"""
    from app.ai import genre_namer

    async def fake_chat(messages, **kwargs):
        return None

    monkeypatch.setattr(genre_namer, "chat_completion", fake_chat)

    labels = await genre_namer.name_genres([("agent",), ("benchmark", "eval")])
    assert labels == ["agent", "benchmark"]


@pytest.mark.asyncio
async def test_name_genres_pads_a_short_llm_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """行数が足りない応答でも、戻り値の長さは入力と必ず一致する。"""
    from app.ai import genre_namer

    async def fake_chat(messages, **kwargs):
        return "エージェント"

    monkeypatch.setattr(genre_namer, "chat_completion", fake_chat)

    labels = await genre_namer.name_genres([("agent",), ("benchmark",)])
    assert labels == ["エージェント", "benchmark"]


@pytest.mark.asyncio
async def test_name_genres_returns_empty_for_no_input() -> None:
    """空入力では LLM を呼ばない。"""
    from app.ai import genre_namer

    assert await genre_namer.name_genres([]) == []
```

- [ ] **Step 2: Run test to verify it fails**

```fish
cd backend && .venv/bin/python -m pytest tests/test_genre_namer.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.ai.genre_namer'`

- [ ] **Step 3: Write minimal implementation**

`backend/app/ai/genre_namer.py`:

```python
"""タグ集合からジャンルの日本語ラベルを付ける。

分類そのものは辞書のみで行い LLM に依存しない（app/services/genre_classifier.py）。
LLM を使うのは提案作成時のラベル命名 1 回だけで、失敗してもタグ名に
フォールバックするので提案の生成は止まらない。
"""

from __future__ import annotations

from app.ai.llm_client import chat_completion

_SYSTEM = (
    "You name RSS article genres in Japanese. For each input line of comma-separated "
    "English tags, output ONE short Japanese genre label (at most 12 characters). "
    "Output exactly one label per input line, in the same order. "
    "No numbering, no quotes, no explanation."
)

# 1 ラベルあたりの余裕を見た上限（日本語 12 文字 + 改行）
_TOKENS_PER_GROUP = 24


async def name_genres(tag_groups: list[tuple[str, ...]]) -> list[str]:
    """各タグ集合に日本語ラベルを付ける。長さは必ず入力と同じ。"""
    from app.ai.task_queue import PRIORITY_FOREGROUND

    if not tag_groups:
        return []

    fallback = [group[0] if group else "" for group in tag_groups]
    lines = ["，".join(group) for group in tag_groups]
    result = await chat_completion(
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": "\n".join(lines)},
        ],
        max_tokens=len(tag_groups) * _TOKENS_PER_GROUP + 256,
        temperature=0.1,
        priority=PRIORITY_FOREGROUND,
        lane="reserved",
    )
    if not result:
        return fallback

    labels = [line.strip() for line in result.splitlines() if line.strip()]
    # 行数がずれた分はタグ名で埋める。提案の子の数と必ず一致させる
    return [labels[i] if i < len(labels) else fallback[i] for i in range(len(tag_groups))]
```

- [ ] **Step 4: Run test to verify it passes**

```fish
cd backend && .venv/bin/python -m pytest tests/test_genre_namer.py -v
```

Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```fish
git add backend/app/ai/genre_namer.py backend/tests/test_genre_namer.py
git commit -m "feat: name proposed genres in Japanese via LLM with tag fallback"
```

---

### Task 7: 設定値とモデル

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/models.py`
- Test: `backend/tests/test_genre_split_store.py`

**Interfaces:**
- Consumes: `app.models.Base` / `_utcnow`（`models.py` 内の既存ヘルパ）
- Produces:
  - `settings.genre_unread_limit: int = 50`（env `SNOREADER_GENRE_UNREAD_LIMIT`）
  - `GenreSplitSuggestion` モデル、テーブル `genre_split_suggestions`、列: `id` int PK / `genre_key` str / `strategy` str / `payload` Text / `before_count` int / `projected_max` int / `created_at` str / `dismissed_at` str? / `dismissed_at_count` int?
  - インデックス `idx_split_suggestions_genre_key`

`before` は SQL 予約語なので列名は `before_count` にする（API では `before` で出す）。

- [ ] **Step 1: Write the failing test**

`backend/tests/test_genre_split_store.py`:

```python
"""分割提案の保存・適用・無視のテスト。LLM は必ずモックする。"""

from __future__ import annotations

import pytest


def test_settings_expose_the_unread_limit() -> None:
    from app.config import Settings

    assert Settings().genre_unread_limit == 50


def test_settings_read_the_limit_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import Settings

    monkeypatch.setenv("SNOREADER_GENRE_UNREAD_LIMIT", "30")
    assert Settings().genre_unread_limit == 30


def test_suggestion_model_columns_exist() -> None:
    from app.models import GenreSplitSuggestion

    columns = set(GenreSplitSuggestion.__table__.columns.keys())
    assert columns == {
        "id",
        "genre_key",
        "strategy",
        "payload",
        "before_count",
        "projected_max",
        "created_at",
        "dismissed_at",
        "dismissed_at_count",
    }
```

- [ ] **Step 2: Run test to verify it fails**

```fish
cd backend && .venv/bin/python -m pytest tests/test_genre_split_store.py -v
```

Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'genre_unread_limit'`

- [ ] **Step 3: Write minimal implementation**

`backend/app/config.py` の `article_retention_days` の次に追加:

```python
    # 葉ジャンルの未読上限。超えると分割案を作る（一括 triage で確認できる上限）
    genre_unread_limit: int = 50
```

`backend/app/models.py` の `GenreRule` クラスの後に追加（`Index` は既に import 済み）:

```python
class GenreSplitSuggestion(Base):
    """未読が上限を超えた葉ジャンルの分割案。

    検知はフィード取得サイクル（1 時間ごと）、閲覧は後から。LaunchAgent は
    make deploy で頻繁に再起動するのでメモリ保持では LLM 命名をやり直すことになる。
    「無視」も永続が必要で、dismissed_at_count より未読が増えたときだけ再提案する。
    """

    __tablename__ = "genre_split_suggestions"
    __table_args__ = (Index("idx_split_suggestions_genre_key", "genre_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 超過していた葉ジャンルのキー。other は genres に行を持たないので FK にはしない
    genre_key: Mapped[str] = mapped_column(String, nullable=False)
    strategy: Mapped[str] = mapped_column(String, nullable=False)
    # SplitProposal の JSON シリアライズ
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    # before は SQL 予約語なので列名を変える（API では before で出す）
    before_count: Mapped[int] = mapped_column(Integer, nullable=False)
    projected_max: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(String, default=_utcnow)
    dismissed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    # 無視した時点の未読件数。これより増えるまで再提案しない
    dismissed_at_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

- [ ] **Step 4: Run test to verify it passes**

```fish
cd backend && .venv/bin/python -m pytest tests/test_genre_split_store.py -v
```

Expected: PASS (3 tests)

新規テーブルは `create_all` が作るので `main.py` の ALTER TABLE 追加は不要。既存 DB でも起動時に作られることを確認:

```fish
cd backend && .venv/bin/python -m pytest tests/test_genres_api.py -v
```

Expected: PASS（既存テストが全部通る）

- [ ] **Step 5: Commit**

```fish
git add backend/app/config.py backend/app/models.py backend/tests/test_genre_split_store.py
git commit -m "feat: add genre unread limit setting and split suggestion model"
```

---

### Task 8: ストア — 未読集計 → プランナ → 提案の upsert

**Files:**
- Create: `backend/app/services/genre_split_store.py`
- Modify: `backend/app/schemas.py`
- Test: `backend/tests/test_genre_split_store.py`

**Interfaces:**
- Consumes: Task 4 の `plan_splits` / `SplitProposal`、Task 6 の `name_genres`、Task 7 の `GenreSplitSuggestion` / `settings.genre_unread_limit`、既存 `genre_classifier.load_rules`
- Produces:
  - `async def refresh_split_suggestions(session: AsyncSession) -> int` — 新規に保存した提案の件数。**commit は呼び出し側**
  - `def proposal_to_payload(proposal: SplitProposal) -> str` / `def payload_to_proposal(payload: str) -> SplitProposal`
  - schemas: `ProposedChildOut(key, label_ja, tags: list[str], estimated_unread)` / `SplitSuggestionOut(id, genre_key, strategy, before, projected_max, children: list[ProposedChildOut], demote_tags: list[str], created_at)`

再提案の規則: 同じ `(genre_key, strategy)` の**保留中**の行があれば作らない。無視済みの行があるときは、現在の未読が `dismissed_at_count` より大きいときだけ作る。

- [ ] **Step 1: Write the failing test**

`backend/tests/test_genre_split_store.py` に追記:

```python
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """test_genres_api.py と同じ作法。lifespan を通して DB を初期化する。"""
    import importlib

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


async def _make_articles(genre_tags: list[tuple[str, int]]) -> None:
    """(タグ JSON, 件数) の指定で未読記事を作る。"""
    import json

    from app.database import async_session
    from app.models import Article, Feed

    async with async_session() as session:
        feed = Feed(title="t", url="http://example.com/feed")
        session.add(feed)
        await session.flush()
        n = 0
        for tags, count in genre_tags:
            for _ in range(count):
                n += 1
                session.add(
                    Article(
                        feed_id=feed.id,
                        guid=f"g{n}",
                        url=f"http://example.com/{n}",
                        title=f"a{n}",
                        tag_suggestions=json.dumps(json.loads(tags)),
                    )
                )
        await session.commit()


@pytest.mark.asyncio
async def test_refresh_stores_a_proposal_for_an_over_limit_genre(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ai タグ 60 件で ai_misc が上限超になり、提案が保存される。"""
    from sqlalchemy import select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import GenreSplitSuggestion
    from app.services.genre_split_store import refresh_split_suggestions

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    # seed_genres + seed_subgenres 後の辞書で ai -> ai_misc になる
    await client.post("/api/genres/seed-subgenres")
    await _make_articles([('["ai", "security"]', 30), ('["ai"]', 30)])

    async with async_session() as session:
        created = await refresh_split_suggestions(session)
        await session.commit()
        rows = (await session.execute(select(GenreSplitSuggestion))).scalars().all()

    assert created > 0
    assert any(r.genre_key == "ai_misc" for r in rows)
    assert all(r.dismissed_at is None for r in rows)
    assert all(r.projected_max <= 50 for r in rows)


@pytest.mark.asyncio
async def test_refresh_is_idempotent_while_a_proposal_is_pending(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """保留中の同じ提案があれば二重に作らない（毎時間つつかない）。"""
    from sqlalchemy import func, select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import GenreSplitSuggestion
    from app.services.genre_split_store import refresh_split_suggestions

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    await client.post("/api/genres/seed-subgenres")
    await _make_articles([('["ai", "security"]', 30), ('["ai"]', 30)])

    async with async_session() as session:
        await refresh_split_suggestions(session)
        await session.commit()
    async with async_session() as session:
        second = await refresh_split_suggestions(session)
        await session.commit()
        total = await session.scalar(select(func.count()).select_from(GenreSplitSuggestion))

    assert second == 0
    assert total is not None and total > 0


@pytest.mark.asyncio
async def test_refresh_makes_no_proposal_when_all_genres_are_small(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ai import genre_namer
    from app.database import async_session
    from app.services.genre_split_store import refresh_split_suggestions

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    await _make_articles([('["ai"]', 5), ('["python"]', 5)])

    async with async_session() as session:
        assert await refresh_split_suggestions(session) == 0
        await session.commit()
```

- [ ] **Step 2: Run test to verify it fails**

```fish
cd backend && .venv/bin/python -m pytest tests/test_genre_split_store.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.genre_split_store'`

- [ ] **Step 3: Write minimal implementation**

`backend/app/schemas.py` の `SeedSubgenresResult` の後に追加:

```python
class ProposedChildOut(BaseModel):
    key: str
    label_ja: str
    tags: list[str]
    estimated_unread: int


class SplitSuggestionOut(BaseModel):
    id: int
    genre_key: str
    strategy: str
    # 検知時の未読件数（モデルの列名は before_count。SQL 予約語を避けている）
    before: int
    projected_max: int
    children: list[ProposedChildOut] = []
    demote_tags: list[str] = []
    created_at: str
```

`backend/app/services/genre_split_store.py`:

```python
"""分割提案の保存・適用・無視。DB 側の入口。

分割の計算そのものは genre_split_planner（DB に触らない純関数）が行う。
ここは未読の集計、LLM によるラベル命名、提案の upsert を担う。
commit は呼び出し側が行う（既存 seed_subgenres と同じ作法）。
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Article, GenreSplitSuggestion


def proposal_to_payload(proposal) -> str:
    """SplitProposal を JSON 文字列にする。"""
    return json.dumps(
        {
            "genre_key": proposal.genre_key,
            "strategy": proposal.strategy,
            "before": proposal.before,
            "projected_max": proposal.projected_max,
            "children": [
                {
                    "key": c.key,
                    "label_ja": c.label_ja,
                    "tags": list(c.tags),
                    "estimated_unread": c.estimated_unread,
                }
                for c in proposal.children
            ],
            "demote_tags": list(proposal.demote_tags),
        },
        ensure_ascii=False,
    )


def payload_to_proposal(payload: str):
    """JSON 文字列から SplitProposal を復元する。"""
    from app.services.genre_split_planner import ProposedChild, SplitProposal

    data = json.loads(payload)
    return SplitProposal(
        genre_key=data["genre_key"],
        strategy=data["strategy"],
        before=data["before"],
        projected_max=data["projected_max"],
        children=tuple(
            ProposedChild(
                key=c["key"],
                label_ja=c["label_ja"],
                tags=tuple(c["tags"]),
                estimated_unread=c["estimated_unread"],
            )
            for c in data["children"]
        ),
        demote_tags=tuple(data["demote_tags"]),
    )


async def _unread_articles(session: AsyncSession) -> list[tuple[int, list[str]]]:
    """未読・未保存・未非表示の記事の (id, タグ) を返す。

    列は 3 つだけ読む（content を含む全列を ORM で読むと本番で 91MB になる）。
    """
    from app.services.genre_classifier import parse_tags

    rows = (
        await session.execute(
            select(Article.id, Article.tag_suggestions).where(
                Article.is_read == False,  # noqa: E712
                Article.is_saved == False,  # noqa: E712
                Article.dismissed_at.is_(None),
                Article.tag_suggestions.isnot(None),
            )
        )
    ).all()
    return [(aid, parse_tags(raw)) for aid, raw in rows]


async def refresh_split_suggestions(session: AsyncSession) -> int:
    """上限超のジャンルを検知し、新しい提案を保存して件数を返す。commit しない。"""
    from app.ai.genre_namer import name_genres
    from app.services.genre_classifier import load_rules
    from app.services.genre_split_planner import plan_splits

    articles = await _unread_articles(session)
    if not articles:
        return 0
    rules = await load_rules(session)
    proposals = plan_splits(articles, rules, limit=settings.genre_unread_limit)
    if not proposals:
        return 0

    existing = (await session.execute(select(GenreSplitSuggestion))).scalars().all()
    pending = {(r.genre_key, r.strategy) for r in existing if r.dismissed_at is None}
    # 無視済みは、そのときの件数より増えたら再提案する
    dismissed_floor: dict[tuple[str, str], int] = {}
    for row in existing:
        if row.dismissed_at is None:
            continue
        key = (row.genre_key, row.strategy)
        floor = row.dismissed_at_count if row.dismissed_at_count is not None else row.before_count
        dismissed_floor[key] = max(dismissed_floor.get(key, 0), floor)

    fresh = []
    for proposal in proposals:
        key = (proposal.genre_key, proposal.strategy)
        if key in pending:
            continue
        if proposal.before <= dismissed_floor.get(key, 0):
            continue
        fresh.append(proposal)
    if not fresh:
        return 0

    # ラベル命名は LLM を 1 回だけ。子を持たない案（demote_generic）は対象外
    groups = [c.tags for p in fresh for c in p.children]
    labels = await name_genres(groups)
    named = iter(labels)
    for proposal in fresh:
        children = tuple(
            type(c)(key=c.key, label_ja=next(named, c.label_ja), tags=c.tags, estimated_unread=c.estimated_unread)
            for c in proposal.children
        )
        stored = type(proposal)(
            genre_key=proposal.genre_key,
            strategy=proposal.strategy,
            before=proposal.before,
            projected_max=proposal.projected_max,
            children=children,
            demote_tags=proposal.demote_tags,
        )
        session.add(
            GenreSplitSuggestion(
                genre_key=stored.genre_key,
                strategy=stored.strategy,
                payload=proposal_to_payload(stored),
                before_count=stored.before,
                projected_max=stored.projected_max,
            )
        )
    await session.flush()
    return len(fresh)
```

- [ ] **Step 4: Run test to verify it passes**

```fish
cd backend && .venv/bin/python -m pytest tests/test_genre_split_store.py -v
```

Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```fish
git add backend/app/services/genre_split_store.py backend/app/schemas.py backend/tests/test_genre_split_store.py
git commit -m "feat: store split suggestions from the planner"
```

---

### Task 9: 適用と無視

**Files:**
- Modify: `backend/app/services/genre_split_store.py`
- Modify: `backend/app/schemas.py`
- Test: `backend/tests/test_genre_split_store.py`

**Interfaces:**
- Consumes: Task 8 の `payload_to_proposal`、既存 `genre_classifier.reclassify_all`、`Genre` / `GenreRule` モデル
- Produces:
  - `async def apply_suggestion(session, suggestion_id: int, *, labels: dict[str, str] | None = None) -> tuple[int, int, int]` — `(created, moved, reclassified)`。`labels` は `{child_key: label_ja}` の上書き。commit は呼び出し側
  - `async def dismiss_suggestion(session, suggestion_id: int) -> int` — 閉じた行数。commit は呼び出し側
  - schemas: `ApplySuggestionBody(labels: dict[str, str] = {})` / `ApplySuggestionResult(created: int, moved: int, reclassified: int)`

適用の規則:
- `demote_tags` のタグは `GenreRule.is_generic = True` にする
- 各 `ProposedChild` について、`key` の `Genre` が無ければ作る。親は `genre_key` の親（`other` のときは `parent_id=None` で `priority=100`）、それ以外は親の `priority` を継ぐ
- 各タグは既存 `GenreRule` があれば `genre_id` を付け替え、無ければ作る（既存 `POST /genre-rules` と同じ「衝突ではなく付け替え」の作法）
- 最後に `reclassify_all()`
- **同じ `genre_key` の保留中の他の案も同時に閉じる**（辞書が変わった後の `projected_max` は無効）

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_apply_creates_children_moves_rules_and_reclassifies(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """適用で子が作られ、ルールが移り、記事が再分類される。"""
    from sqlalchemy import select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import Article, Genre, GenreSplitSuggestion
    from app.services.genre_split_store import (
        apply_suggestion,
        payload_to_proposal,
        refresh_split_suggestions,
    )

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    await client.post("/api/genres/seed-subgenres")
    # ai + 未ルールタグ agent の記事を多く作り、promote_free_tags が成立する状況にする
    await _make_articles([('["ai", "agent"]', 30), ('["ai"]', 30)])

    async with async_session() as session:
        await refresh_split_suggestions(session)
        await session.commit()

    async with async_session() as session:
        rows = (await session.execute(select(GenreSplitSuggestion))).scalars().all()
        target = next(r for r in rows if r.strategy in {"promote_free_tags", "demote_generic"})
        proposal = payload_to_proposal(target.payload)
        created, moved, reclassified = await apply_suggestion(session, target.id)
        await session.commit()

    async with async_session() as session:
        # demote_generic なら子は増えない。promote_free_tags なら子が増える
        if proposal.children:
            assert created == len(proposal.children)
            keys = {
                g.key for g in (await session.execute(select(Genre))).scalars().all()
            }
            assert {c.key for c in proposal.children} <= keys
        else:
            assert created == 0
            assert moved > 0
        # 適用したら記事の genre が実際に動いている
        assert reclassified > 0
        assert (await session.execute(select(Article))).scalars().first() is not None
        # 適用した行は閉じている
        applied = await session.get(GenreSplitSuggestion, target.id)
        assert applied is not None and applied.dismissed_at is not None


@pytest.mark.asyncio
async def test_apply_overrides_child_labels(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """承認時に編集したラベルが使われる。"""
    from sqlalchemy import select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import Genre, GenreSplitSuggestion
    from app.services.genre_split_store import (
        apply_suggestion,
        payload_to_proposal,
        refresh_split_suggestions,
    )

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    await client.post("/api/genres/seed-subgenres")
    await _make_articles([('["ai", "agent"]', 30), ('["ai"]', 30)])

    async with async_session() as session:
        await refresh_split_suggestions(session)
        await session.commit()

    async with async_session() as session:
        rows = (await session.execute(select(GenreSplitSuggestion))).scalars().all()
        target = next(r for r in rows if payload_to_proposal(r.payload).children)
        child_key = payload_to_proposal(target.payload).children[0].key
        await apply_suggestion(session, target.id, labels={child_key: "AIエージェント"})
        await session.commit()

    async with async_session() as session:
        genre = (
            await session.execute(select(Genre).where(Genre.key == child_key))
        ).scalar_one()
        assert genre.label_ja == "AIエージェント"


@pytest.mark.asyncio
async def test_apply_closes_the_other_pending_proposals_for_the_same_genre(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1 つ適用すると、同ジャンルの他の案も閉じる（projected_max が無効になるため）。"""
    from sqlalchemy import select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import GenreSplitSuggestion
    from app.services.genre_split_store import apply_suggestion, refresh_split_suggestions

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    await client.post("/api/genres/seed-subgenres")
    await _make_articles([('["ai", "agent"]', 30), ('["ai", "security"]', 30), ('["ai"]', 10)])

    async with async_session() as session:
        await refresh_split_suggestions(session)
        await session.commit()

    async with async_session() as session:
        rows = (await session.execute(select(GenreSplitSuggestion))).scalars().all()
        same_genre = [r for r in rows if r.genre_key == rows[0].genre_key]
        if len(same_genre) < 2:
            pytest.skip("この辞書では同ジャンルに複数案が立たなかった")
        await apply_suggestion(session, same_genre[0].id)
        await session.commit()

    async with async_session() as session:
        after = (await session.execute(select(GenreSplitSuggestion))).scalars().all()
        for row in after:
            if row.genre_key == same_genre[0].genre_key:
                assert row.dismissed_at is not None


@pytest.mark.asyncio
async def test_dismiss_suppresses_until_the_count_grows(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """無視した後は、未読がその時点より増えるまで再提案されない。"""
    from sqlalchemy import select

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import GenreSplitSuggestion
    from app.services.genre_split_store import (
        dismiss_suggestion,
        refresh_split_suggestions,
    )

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    await client.post("/api/genres/seed-subgenres")
    await _make_articles([('["ai", "agent"]', 30), ('["ai"]', 30)])

    async with async_session() as session:
        await refresh_split_suggestions(session)
        await session.commit()
    async with async_session() as session:
        rows = (await session.execute(select(GenreSplitSuggestion))).scalars().all()
        for row in rows:
            await dismiss_suggestion(session, row.id)
        await session.commit()

    # 件数が変わらないので再提案されない
    async with async_session() as session:
        assert await refresh_split_suggestions(session) == 0
        await session.commit()

    # 未読が増えたら再提案される
    await _make_articles([('["ai"]', 20)])
    async with async_session() as session:
        assert await refresh_split_suggestions(session) > 0
        await session.commit()
```

- [ ] **Step 2: Run test to verify it fails**

```fish
cd backend && .venv/bin/python -m pytest tests/test_genre_split_store.py -v
```

Expected: FAIL — `ImportError: cannot import name 'apply_suggestion' from 'app.services.genre_split_store'`

- [ ] **Step 3: Write minimal implementation**

`backend/app/schemas.py` に追加:

```python
class ApplySuggestionBody(BaseModel):
    # {child_key: label_ja} の上書き。承認ダイアログで直した名前が入る
    labels: dict[str, str] = {}


class ApplySuggestionResult(BaseModel):
    created: int
    moved: int
    reclassified: int
```

`genre_split_store.py` に追加（`Genre` / `GenreRule` を top-level import に足す）:

```python
def _utcnow() -> str:
    from app.models import _utcnow as models_utcnow

    return models_utcnow()


async def _close_pending_for_genre(session: AsyncSession, genre_key: str, count: int) -> int:
    """そのジャンルの保留中の提案を全部閉じる。閉じた行数を返す。"""
    rows = (
        await session.execute(
            select(GenreSplitSuggestion).where(
                GenreSplitSuggestion.genre_key == genre_key,
                GenreSplitSuggestion.dismissed_at.is_(None),
            )
        )
    ).scalars().all()
    now = _utcnow()
    for row in rows:
        row.dismissed_at = now
        row.dismissed_at_count = count
    return len(rows)


async def apply_suggestion(
    session: AsyncSession,
    suggestion_id: int,
    *,
    labels: dict[str, str] | None = None,
) -> tuple[int, int, int]:
    """提案を適用する。(created, moved, reclassified) を返す。commit しない。"""
    from app.models import Genre, GenreRule
    from app.services.genre_classifier import OTHER_GENRE, reclassify_all

    row = await session.get(GenreSplitSuggestion, suggestion_id)
    if row is None:
        raise LookupError("Suggestion not found")
    proposal = payload_to_proposal(row.payload)
    overrides = labels or {}

    created = 0
    moved = 0

    # 受け皿タグの汎用降格
    for tag in proposal.demote_tags:
        rule = (
            await session.execute(select(GenreRule).where(GenreRule.tag == tag))
        ).scalar_one_or_none()
        if rule is not None and not rule.is_generic:
            rule.is_generic = True
            moved += 1

    # 新しい子（または other 由来の新トップレベル）
    if proposal.children:
        is_other = proposal.genre_key == OTHER_GENRE
        parent_id: int | None = None
        priority = 100
        if not is_other:
            target = (
                await session.execute(select(Genre).where(Genre.key == proposal.genre_key))
            ).scalar_one_or_none()
            if target is None:
                raise LookupError("Genre no longer exists")
            # 対象が子なら同じ親の下に、対象が親なら対象の下に置く（階層は 2 段のまま）
            parent_id = target.parent_id if target.parent_id is not None else target.id
            parent = await session.get(Genre, parent_id)
            priority = parent.priority if parent else target.priority

        for child in proposal.children:
            genre = (
                await session.execute(select(Genre).where(Genre.key == child.key))
            ).scalar_one_or_none()
            label = overrides.get(child.key, child.label_ja)
            if genre is None:
                genre = Genre(
                    key=child.key,
                    label_ja=label,
                    priority=priority,
                    parent_id=parent_id,
                )
                session.add(genre)
                await session.flush()
                created += 1
            else:
                genre.label_ja = label

            for tag in child.tags:
                rule = (
                    await session.execute(select(GenreRule).where(GenreRule.tag == tag))
                ).scalar_one_or_none()
                if rule is None:
                    # 既存 POST /genre-rules と同じ「衝突ではなく付け替え」の作法
                    session.add(GenreRule(tag=tag, genre_id=genre.id, is_generic=False))
                    moved += 1
                elif rule.genre_id != genre.id:
                    rule.genre_id = genre.id
                    moved += 1

    await session.flush()
    reclassified = await reclassify_all(session)
    # 辞書が変わった後は同ジャンルの他の案の projected_max が無効になるので全部閉じる
    await _close_pending_for_genre(session, proposal.genre_key, proposal.before)
    return created, moved, reclassified


async def dismiss_suggestion(session: AsyncSession, suggestion_id: int) -> int:
    """その提案とその同ジャンルの保留を全部閉じる。閉じた行数を返す。commit しない。"""
    row = await session.get(GenreSplitSuggestion, suggestion_id)
    if row is None:
        raise LookupError("Suggestion not found")
    return await _close_pending_for_genre(session, row.genre_key, row.before_count)
```

- [ ] **Step 4: Run test to verify it passes**

```fish
cd backend && .venv/bin/python -m pytest tests/test_genre_split_store.py -v
```

Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```fish
git add backend/app/services/genre_split_store.py backend/app/schemas.py backend/tests/test_genre_split_store.py
git commit -m "feat: apply and dismiss genre split suggestions"
```

---

### Task 10: エンドポイント

**Files:**
- Modify: `backend/app/routers/genres.py`
- Test: `backend/tests/test_genres_api.py`

**Interfaces:**
- Consumes: Task 9 の `apply_suggestion` / `dismiss_suggestion`、Task 8 の `refresh_split_suggestions` / `payload_to_proposal`、schemas `SplitSuggestionOut` / `ProposedChildOut` / `ApplySuggestionBody` / `ApplySuggestionResult`
- Produces:
  - `GET /api/genres/split-suggestions` → `list[SplitSuggestionOut]`（保留中のみ、`projected_max` 昇順）
  - `POST /api/genres/split-suggestions/refresh` → `dict` `{"created": int}`
  - `POST /api/genres/split-suggestions/{id}/apply` → `ApplySuggestionResult`（提案が無ければ 404）
  - `POST /api/genres/split-suggestions/{id}/dismiss` → `dict` `{"dismissed": int}`

**重要:** ルートの登録順。`/genres/split-suggestions` は既存の `/genres/{genre_id}` より**前**に定義しないと、`split-suggestions` が `genre_id` として解釈されて 422 になる。既存ファイル内の `@router.get("/genres/...")` 群を確認して、パス変数を持つルートより上に置く。

- [ ] **Step 1: Write the failing test**

`backend/tests/test_genres_api.py` の末尾に追記:

```python
@pytest.mark.asyncio
async def test_split_suggestion_endpoints_list_apply_and_dismiss(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提案の一覧・適用・無視が API で通ること。LLM 命名はモックする。"""
    import json

    from app.ai import genre_namer
    from app.database import async_session
    from app.models import Article, Feed

    async def fake_name(tag_groups):
        return [g[0] if g else "" for g in tag_groups]

    monkeypatch.setattr(genre_namer, "name_genres", fake_name)

    await client.post("/api/genres/seed-subgenres")

    async with async_session() as session:
        feed = Feed(title="t", url="http://example.com/feed")
        session.add(feed)
        await session.flush()
        for n in range(60):
            tags = ["ai", "agent"] if n < 30 else ["ai"]
            session.add(
                Article(
                    feed_id=feed.id,
                    guid=f"g{n}",
                    url=f"http://example.com/{n}",
                    title=f"a{n}",
                    tag_suggestions=json.dumps(tags),
                )
            )
        await session.commit()

    refreshed = await client.post("/api/genres/split-suggestions/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["created"] > 0

    listed = await client.get("/api/genres/split-suggestions")
    assert listed.status_code == 200
    items = listed.json()
    assert items
    # projected_max 昇順
    assert [i["projected_max"] for i in items] == sorted(i["projected_max"] for i in items)
    first = items[0]
    assert first["before"] > 50
    assert "children" in first and "demote_tags" in first

    applied = await client.post(f"/api/genres/split-suggestions/{first['id']}/apply", json={})
    assert applied.status_code == 200
    body = applied.json()
    assert set(body) == {"created", "moved", "reclassified"}

    # 適用したら一覧から消える
    after = await client.get("/api/genres/split-suggestions")
    assert all(i["id"] != first["id"] for i in after.json())


@pytest.mark.asyncio
async def test_apply_unknown_suggestion_returns_404(client: AsyncClient) -> None:
    res = await client.post("/api/genres/split-suggestions/9999/apply", json={})
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_dismiss_unknown_suggestion_returns_404(client: AsyncClient) -> None:
    res = await client.post("/api/genres/split-suggestions/9999/dismiss")
    assert res.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

```fish
cd backend && .venv/bin/python -m pytest tests/test_genres_api.py -k split -v
```

Expected: FAIL — 404 / 405（ルートが未定義）

- [ ] **Step 3: Write minimal implementation**

`backend/app/routers/genres.py` の import に追加:

```python
from app.models import Genre, GenreRule, GenreSplitSuggestion
from app.schemas import (
    ApplySuggestionBody,
    ApplySuggestionResult,
    GenreCreate,
    GenreOut,
    GenreRuleCreate,
    GenreRuleOut,
    GenreUpdate,
    ProposedChildOut,
    ReclassifyResult,
    SeedSubgenresResult,
    SplitSuggestionOut,
)
```

**パス変数を持つ `/genres/{...}` ルートより前**に以下を追加:

```python
@router.get("/genres/split-suggestions", response_model=list[SplitSuggestionOut])
async def list_split_suggestions(session: AsyncSession = Depends(get_session)):
    """保留中の分割提案を projected_max 昇順で返す。"""
    from app.services.genre_split_store import payload_to_proposal

    rows = (
        await session.execute(
            select(GenreSplitSuggestion)
            .where(GenreSplitSuggestion.dismissed_at.is_(None))
            .order_by(GenreSplitSuggestion.projected_max.asc(), GenreSplitSuggestion.id.asc())
        )
    ).scalars().all()

    out = []
    for row in rows:
        proposal = payload_to_proposal(row.payload)
        out.append(
            SplitSuggestionOut(
                id=row.id,
                genre_key=row.genre_key,
                strategy=row.strategy,
                before=row.before_count,
                projected_max=row.projected_max,
                children=[
                    ProposedChildOut(
                        key=c.key,
                        label_ja=c.label_ja,
                        tags=list(c.tags),
                        estimated_unread=c.estimated_unread,
                    )
                    for c in proposal.children
                ],
                demote_tags=list(proposal.demote_tags),
                created_at=row.created_at,
            )
        )
    return out


@router.post("/genres/split-suggestions/refresh", response_model=dict)
async def refresh_split_suggestions_endpoint(session: AsyncSession = Depends(get_session)):
    """手動で再計算する。通常はフィード取得サイクルの末尾で走る。"""
    from app.services.genre_split_store import refresh_split_suggestions

    created = await refresh_split_suggestions(session)
    await session.commit()
    return {"created": created}


@router.post("/genres/split-suggestions/{suggestion_id}/apply", response_model=ApplySuggestionResult)
async def apply_split_suggestion(
    suggestion_id: int,
    body: ApplySuggestionBody,
    session: AsyncSession = Depends(get_session),
):
    """提案を適用する。子作成 / ルール移動 / 汎用降格 → 全件再分類（実測 47 秒）。"""
    from app.services.genre_split_store import apply_suggestion

    try:
        created, moved, reclassified = await apply_suggestion(
            session, suggestion_id, labels=body.labels
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return ApplySuggestionResult(created=created, moved=moved, reclassified=reclassified)


@router.post("/genres/split-suggestions/{suggestion_id}/dismiss", response_model=dict)
async def dismiss_split_suggestion(
    suggestion_id: int, session: AsyncSession = Depends(get_session)
):
    """その提案と同ジャンルの保留を閉じる。未読がこの時点より増えるまで再提案しない。"""
    from app.services.genre_split_store import dismiss_suggestion

    try:
        dismissed = await dismiss_suggestion(session, suggestion_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return {"dismissed": dismissed}
```

- [ ] **Step 4: Run tests**

```fish
cd backend && .venv/bin/python -m pytest tests/test_genres_api.py -v
```

Expected: PASS（既存 + 新規 3 件）

```fish
cd backend && .venv/bin/python -m pytest
```

Expected: 全件 PASS

- [ ] **Step 5: Commit**

```fish
git add backend/app/routers/genres.py backend/tests/test_genres_api.py
git commit -m "feat: expose split suggestion endpoints"
```

---

### Task 11: フィード取得サイクルへの組み込み

**Files:**
- Modify: `backend/app/services/feed_fetcher.py:171-173`
- Test: `backend/tests/test_genre_split_store.py`

**Interfaces:**
- Consumes: Task 8 の `refresh_split_suggestions`
- Produces: なし（`fetch_all_feeds()` の副作用が増えるだけ）

`fetch_all_feeds()` は現在末尾で `dedup_articles(session)` → `cleanup_old_articles(session)` を呼ぶ。その後に `refresh_split_suggestions(session)` と commit を足す。**提案生成で例外が出てもフィード取得を落とさない**（取得の方が重要な機能）。

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_fetch_all_feeds_refreshes_split_suggestions(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """フィード取得サイクルの末尾で提案が更新される。"""
    called: list[bool] = []

    from app.services import feed_fetcher, genre_split_store

    async def fake_refresh(session):
        called.append(True)
        return 0

    monkeypatch.setattr(genre_split_store, "refresh_split_suggestions", fake_refresh)
    await feed_fetcher.fetch_all_feeds()

    assert called == [True]


@pytest.mark.asyncio
async def test_fetch_all_feeds_survives_a_failing_refresh(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提案生成が落ちてもフィード取得は失敗しない（取得の方が重要）。"""
    from app.services import feed_fetcher, genre_split_store

    async def boom(session):
        raise RuntimeError("planner exploded")

    monkeypatch.setattr(genre_split_store, "refresh_split_suggestions", boom)
    await feed_fetcher.fetch_all_feeds()  # 例外が漏れないこと
```

- [ ] **Step 2: Run test to verify it fails**

```fish
cd backend && .venv/bin/python -m pytest tests/test_genre_split_store.py -k fetch_all -v
```

Expected: FAIL — `assert called == [True]` が `[]` で落ちる

- [ ] **Step 3: Write minimal implementation**

`backend/app/services/feed_fetcher.py` の `fetch_all_feeds()` 末尾を差し替え:

```python
    async with async_session() as session:
        await dedup_articles(session)
        await cleanup_old_articles(session)

    # 未読が増えるのはフィードを取得した瞬間だけなので、上限超の検知はここで 1 回。
    # ここが失敗しても取得サイクル自体は成功扱いにする（取得の方が重要な機能）
    from app.services import genre_split_store

    async with async_session() as session:
        try:
            await genre_split_store.refresh_split_suggestions(session)
            await session.commit()
        except Exception:
            logger.exception("Failed to refresh genre split suggestions")
```

`logger` が未定義なら、ファイル冒頭の既存ロガー定義を確認して合わせる（無ければ `import logging` / `logger = logging.getLogger(__name__)` を追加）。

**注意:** `monkeypatch.setattr(genre_split_store, "refresh_split_suggestions", ...)` が効くよう、`genre_split_store` モジュールを import して**属性経由で呼ぶ**（`from ... import refresh_split_suggestions` にすると差し替えが効かない）。

- [ ] **Step 4: Run tests**

```fish
cd backend && .venv/bin/python -m pytest tests/test_genre_split_store.py -v
cd backend && .venv/bin/python -m pytest
```

Expected: 全件 PASS

- [ ] **Step 5: Commit**

```fish
git add backend/app/services/feed_fetcher.py backend/tests/test_genre_split_store.py
git commit -m "feat: refresh split suggestions at the end of each fetch cycle"
```

---

### Task 12: フロントエンド — 型・API クライアント・フック

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api/client.ts`
- Create: `frontend/src/hooks/useSplitSuggestions.ts`

**Interfaces:**
- Consumes: Task 10 の 4 エンドポイント
- Produces:
  - types: `ProposedChild { key: string; label_ja: string; tags: string[]; estimated_unread: number }` / `SplitSuggestion { id: number; genre_key: string; strategy: string; before: number; projected_max: number; children: ProposedChild[]; demote_tags: string[]; created_at: string }`
  - client: `getSplitSuggestions(): Promise<SplitSuggestion[]>` / `refreshSplitSuggestions(): Promise<{ created: number }>` / `applySplitSuggestion(id: number, labels: Record<string, string>): Promise<{ created: number; moved: number; reclassified: number }>` / `dismissSplitSuggestion(id: number): Promise<{ dismissed: number }>`
  - hooks: `useSplitSuggestions()` / `useApplySplitSuggestion()` / `useDismissSplitSuggestion()` / `useRefreshSplitSuggestions()`

適用は辞書と記事分類を変えるので、`useGenres.ts` の `useInvalidateGenreDefs` と同じ 3 つのキー（`genres` / `genre-counts` / `articles`）＋ `split-suggestions` を無効化する。

- [ ] **Step 1: 型を追加**

`frontend/src/types.ts` の末尾に追加:

```typescript
export interface ProposedChild {
  key: string;
  label_ja: string;
  tags: string[];
  estimated_unread: number;
}

// 未読が上限を超えた葉ジャンルの分割案。件数はバックエンドで実際に分類し直した実測値
export interface SplitSuggestion {
  id: number;
  genre_key: string;
  strategy: string;
  before: number;
  projected_max: number;
  children: ProposedChild[];
  demote_tags: string[];
  created_at: string;
}
```

- [ ] **Step 2: API クライアント関数を追加**

`frontend/src/api/client.ts` の末尾に追加（既存の `fetchJSON` の使い方に合わせる。既存の `seedSubgenres` などの書き方を確認してから）:

```typescript
export const getSplitSuggestions = () =>
  fetchJSON<SplitSuggestion[]>('/genres/split-suggestions');

export const refreshSplitSuggestions = () =>
  fetchJSON<{ created: number }>('/genres/split-suggestions/refresh', { method: 'POST' });

export const applySplitSuggestion = (id: number, labels: Record<string, string>) =>
  fetchJSON<{ created: number; moved: number; reclassified: number }>(
    `/genres/split-suggestions/${id}/apply`,
    { method: 'POST', body: JSON.stringify({ labels }) },
  );

export const dismissSplitSuggestion = (id: number) =>
  fetchJSON<{ dismissed: number }>(`/genres/split-suggestions/${id}/dismiss`, { method: 'POST' });
```

`SplitSuggestion` の import を `client.ts` 冒頭の型 import に追加する。

- [ ] **Step 3: フックを作る**

`frontend/src/hooks/useSplitSuggestions.ts`:

```typescript
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import * as api from '../api/client';

export function useSplitSuggestions() {
  return useQuery({
    queryKey: ['split-suggestions'],
    queryFn: api.getSplitSuggestions,
    staleTime: 60_000,
  });
}

// 適用は辞書と記事分類を両方変えるので、ジャンル系と記事のキャッシュを全部捨てる
function useInvalidateAfterApply() {
  const qc = useQueryClient();
  return () => {
    qc.invalidateQueries({ queryKey: ['split-suggestions'] });
    qc.invalidateQueries({ queryKey: ['genres'] });
    qc.invalidateQueries({ queryKey: ['genre-counts'] });
    qc.invalidateQueries({ queryKey: ['articles'] });
  };
}

export function useApplySplitSuggestion() {
  const invalidate = useInvalidateAfterApply();
  return useMutation({
    mutationFn: ({ id, labels }: { id: number; labels: Record<string, string> }) =>
      api.applySplitSuggestion(id, labels),
    onSuccess: invalidate,
  });
}

export function useDismissSplitSuggestion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.dismissSplitSuggestion(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['split-suggestions'] }),
  });
}

export function useRefreshSplitSuggestions() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.refreshSplitSuggestions,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['split-suggestions'] }),
  });
}
```

- [ ] **Step 4: 型チェック**

```fish
cd frontend && npx tsc --noEmit
```

Expected: エラーなし

- [ ] **Step 5: Commit**

```fish
git add frontend/src/types.ts frontend/src/api/client.ts frontend/src/hooks/useSplitSuggestions.ts
git commit -m "feat: add split suggestion types, client and hooks"
```

---

### Task 13: フロントエンド — 提案パネルとバッジ

**Files:**
- Create: `frontend/src/components/layout/SplitSuggestionPanel.tsx`
- Modify: `frontend/src/components/layout/GenreManagerModal.tsx`
- Modify: `frontend/src/components/layout/FeedSidebar.tsx`

**Interfaces:**
- Consumes: Task 12 の `useSplitSuggestions` / `useApplySplitSuggestion` / `useDismissSplitSuggestion` / `useRefreshSplitSuggestions`、型 `SplitSuggestion`
- Produces: `export function SplitSuggestionPanel(): JSX.Element | null`（props なし。提案が 0 件なら `null`）

パネルの表示要素（spec より）:
- 見出し「⚠ {genre_key} の未読が {before} 件（上限 50）」
- 案ごとに strategy の日本語名、`{before} → {projected_max}` の件数
- `demote_generic` は「ルール `ai` を汎用に降格」と出す（子は無い）
- 子を持つ案は各子の `key` / タグ / `estimated_unread` と**編集可能なラベル入力**
- `[適用]`（`confirm` で「既存記事の再分類に十数秒かかります」を出す。既存 seed-subgenres ボタンと同じ作法）と `[無視]`

- [ ] **Step 1: パネルを作る**

`frontend/src/components/layout/SplitSuggestionPanel.tsx`:

```typescript
import { useState } from 'react';
import {
  useSplitSuggestions,
  useApplySplitSuggestion,
  useDismissSplitSuggestion,
  useRefreshSplitSuggestions,
} from '../../hooks/useSplitSuggestions';
import type { SplitSuggestion } from '../../types';

const STRATEGY_LABEL: Record<string, string> = {
  demote_generic: 'タグを汎用ルールに降格（ジャンルを増やさない）',
  split_own_tags: '担当タグを兄弟ジャンルに分ける',
  promote_free_tags: '未ルールのタグを兄弟ジャンルに昇格',
};

// 分割案の提示と適用。件数はバックエンドで実際に分類し直した実測値なので、
// ここでは計算せず表示するだけ。
export function SplitSuggestionPanel() {
  const { data: suggestions } = useSplitSuggestions();
  const apply = useApplySplitSuggestion();
  const dismiss = useDismissSplitSuggestion();
  const refresh = useRefreshSplitSuggestions();
  // {suggestionId: {childKey: label}} の編集中の値
  const [labels, setLabels] = useState<Record<number, Record<string, string>>>({});
  const [lastResult, setLastResult] = useState<string | null>(null);

  const items = suggestions ?? [];

  const editedLabels = (s: SplitSuggestion) => {
    const edited = labels[s.id] ?? {};
    const out: Record<string, string> = {};
    for (const child of s.children) out[child.key] = edited[child.key] ?? child.label_ja;
    return out;
  };

  return (
    <div className="space-y-2 px-1">
      <div className="flex items-center gap-2">
        <span className="text-xs font-medium text-gray-600 dark:text-gray-300">
          ジャンル分割の提案
        </span>
        <div className="flex-1" />
        <button
          type="button"
          className="text-xs text-blue-600 hover:underline disabled:opacity-50 dark:text-blue-400"
          disabled={refresh.isPending}
          onClick={() => refresh.mutate()}
        >
          再計算
        </button>
      </div>

      {items.length === 0 && (
        <p className="text-xs text-gray-500 dark:text-gray-400">
          未読が上限を超えたジャンルはありません。
        </p>
      )}

      {items.map((s) => (
        <div
          key={s.id}
          className="rounded border border-amber-300 bg-amber-50 p-2 text-xs dark:border-amber-700 dark:bg-amber-950"
        >
          <div className="font-medium text-amber-900 dark:text-amber-200">
            ⚠ {s.genre_key} の未読が {s.before} 件
          </div>
          <div className="mt-1 text-gray-700 dark:text-gray-300">
            {STRATEGY_LABEL[s.strategy] ?? s.strategy}
            {' — '}
            最大バケット {s.before} → {s.projected_max}
          </div>

          {s.demote_tags.length > 0 && (
            <div className="mt-1 text-gray-600 dark:text-gray-400">
              降格するタグ: {s.demote_tags.join(', ')}
            </div>
          )}

          {s.children.map((child) => (
            <div key={child.key} className="mt-1 flex items-center gap-1">
              <input
                className="w-28 rounded border border-gray-300 px-1 py-0.5 dark:border-gray-600 dark:bg-gray-800"
                value={(labels[s.id] ?? {})[child.key] ?? child.label_ja}
                onChange={(e) =>
                  setLabels((prev) => ({
                    ...prev,
                    [s.id]: { ...(prev[s.id] ?? {}), [child.key]: e.target.value },
                  }))
                }
              />
              <span className="text-gray-600 dark:text-gray-400">
                {child.key} ({child.tags.join(', ')}) — {child.estimated_unread} 件
              </span>
            </div>
          ))}

          <div className="mt-2 flex items-center gap-2">
            <button
              type="button"
              className="rounded bg-blue-600 px-2 py-0.5 text-white hover:bg-blue-700 disabled:opacity-50"
              disabled={apply.isPending}
              onClick={() => {
                if (!confirm('この案を適用します。既存記事の再分類に十数秒かかります。')) return;
                apply.mutate(
                  { id: s.id, labels: editedLabels(s) },
                  {
                    onSuccess: (r) =>
                      setLastResult(
                        `ジャンル ${r.created} 件作成 / ルール ${r.moved} 件変更 / 記事 ${r.reclassified} 件再分類`,
                      ),
                  },
                );
              }}
            >
              適用
            </button>
            <button
              type="button"
              className="rounded border border-gray-300 px-2 py-0.5 hover:bg-gray-100 disabled:opacity-50 dark:border-gray-600 dark:hover:bg-gray-800"
              disabled={dismiss.isPending}
              onClick={() => dismiss.mutate(s.id)}
            >
              無視
            </button>
          </div>
        </div>
      ))}

      {lastResult && (
        <p className="text-xs text-gray-500 dark:text-gray-400">{lastResult}</p>
      )}
    </div>
  );
}
```

- [ ] **Step 2: GenreManagerModal に差し込む**

`GenreManagerModal.tsx` の import に追加:

```typescript
import { SplitSuggestionPanel } from './SplitSuggestionPanel';
```

`return (` 内の最上部（`<div className="space-y-1 px-1">` の直後）に挿入:

```tsx
      <SplitSuggestionPanel />
```

- [ ] **Step 3: サイドバーにバッジを足す**

`FeedSidebar.tsx` の「ジャンル管理」を開くボタン／ジャンル節の見出しに、保留件数のバッジを付ける。フックを import:

```typescript
import { useSplitSuggestions } from '../../hooks/useSplitSuggestions';
```

コンポーネント内:

```typescript
  const { data: splitSuggestions } = useSplitSuggestions();
  const pendingSplits = splitSuggestions?.length ?? 0;
```

ジャンル節の見出し（既存のジャンル一覧のラベル）の隣に:

```tsx
        {pendingSplits > 0 && (
          <span
            className="ml-1 rounded-full bg-amber-500 px-1.5 text-[10px] font-medium text-white"
            title={`未読が上限を超えたジャンルの分割提案が ${pendingSplits} 件あります`}
          >
            {pendingSplits}
          </span>
        )}
```

既存のジャンル節の JSX 構造を読んでから、見出し要素の中に入れる。

- [ ] **Step 4: 型チェックとビルド**

```fish
cd frontend && npx tsc --noEmit
cd frontend && npm run lint
cd frontend && npm run build
```

Expected: すべてエラーなし

- [ ] **Step 5: Commit**

```fish
git add frontend/src/components/layout/SplitSuggestionPanel.tsx frontend/src/components/layout/GenreManagerModal.tsx frontend/src/components/layout/FeedSidebar.tsx
git commit -m "feat: show genre split suggestions in the genre manager"
```

---

### Task 14: 実機確認・ドキュメント・リリース

**Files:**
- Modify: `CLAUDE.md`（Genre triage 節に自動分割の説明を追記）
- Modify: `README.md` / `README.ja.md`
- Modify: `backend/pyproject.toml` / `frontend/package.json`（バージョン）
- Modify: `backend/uv.lock` / `frontend/package-lock.json`

**Interfaces:**
- Consumes: Task 1-13 の全成果
- Produces: リリースされた `v0.13.0`

新機能なので minor を上げる（現在 v0.12.16 → **v0.13.0**）。

- [ ] **Step 1: 全テストと型チェック**

```fish
cd backend && .venv/bin/python -m pytest
cd frontend && npx tsc --noEmit
cd frontend && npm run lint
cd frontend && npm run build
```

Expected: すべて PASS

- [ ] **Step 2: 実機で確認**

```fish
make dev
```

ブラウザで確認する項目:
1. サイドバー → ジャンル管理を開くと提案パネルが出る（提案 0 件なら「未読が上限を超えたジャンルはありません」）
2. 「再計算」を押すと提案が出る（本番相当のデータなら `ai_misc` に対する `demote_generic` 案が出るはず）
3. ラベル入力を編集して「適用」→ 確認ダイアログ → 結果メッセージが出る
4. サイドバーのジャンル件数が更新され、どのジャンルも 50 以下になっている
5. 「無視」を押すと提案が消え、「再計算」しても戻ってこない
6. ダークモードで見出し・バッジ・ボタンが読める

- [ ] **Step 3: ドキュメントを更新**

`CLAUDE.md` の「Genre triage」節の末尾に追記:

```markdown
- **Auto-split suggestions** (`genre_split_planner.py` / `genre_split_store.py`): when a *leaf* genre's unread count exceeds `SNOREADER_GENRE_UNREAD_LIMIT` (default 50), `fetch_all_feeds()` stores split proposals in `genre_split_suggestions`. Proposals are *suggested*, never applied automatically — applying rewrites the dictionary and costs a full `reclassify_all` (47s in production), and silently changing the user's dictionary is the same failure mode `POST /genres/seed-subgenres` avoids by not running at startup. Three strategies, all validated by re-running the real `classify` over the unread set rather than estimating: `demote_generic` (make a receptacle tag like `ai` generic so competing genres win — measured to take `ai_misc` from 53 to 17 with zero buckets over 50, without adding any genre), `split_own_tags` (bin-pack a genre's own tags into new siblings), `promote_free_tags` (adopt unruled co-occurring tags, floor of `_MIN_CHILD_ARTICLES=8` so 2-article genres are never proposed). The simulation is what catches the sibling-key trap documented in `genre_seed.py`: siblings share the parent's priority, so a new sibling whose key sorts *after* the receptacle takes zero articles — such candidates come back with a projected count of 0 and are dropped. The reverse operation (merging shrunken genres back) is deliberately not implemented; see the spec.
```

`README.md` / `README.ja.md` の機能一覧に 1 行足す（日本語版が英語版のミラーであることを確認して両方に）。

- [ ] **Step 4: バージョンを上げて lock を作り直す**

```fish
cd backend && sed -i '' 's/^version = "0.12.16"/version = "0.13.0"/' pyproject.toml && uv lock
cd frontend && npm version 0.13.0 --no-git-tag-version && npm install --package-lock-only
```

確認:

```fish
grep '^version' backend/pyproject.toml
grep '"version"' frontend/package.json | head -1
```

Expected: 両方 `0.13.0`

- [ ] **Step 5: コミット・PR・マージ・タグ・デプロイ**

```fish
git add CLAUDE.md README.md README.ja.md backend/pyproject.toml backend/uv.lock frontend/package.json frontend/package-lock.json
git commit -m "docs: document genre auto-split (v0.13.0)"
git push -u origin feat/genre-auto-split
gh pr create --title "feat: suggest genre splits when a leaf genre's unread exceeds 50" --body "<Summary / Test plan>"
```

PR がグリーンになったら `AGENTS.md` の手順どおり:

```fish
git checkout main
git merge --no-ff feat/genre-auto-split
git tag v0.13.0
git push origin main --tags
make deploy
launchctl kickstart -k "gui/$(id -u)/com.ccxa.snoreader"
launchctl list | grep snoreader   # 新 PID を確認
curl -s http://localhost:8000/api/genres/split-suggestions | head -c 400
```

Expected: 新しい PID が出て、エンドポイントが JSON を返す

---

## Self-Review

**1. Spec coverage**

| spec の項目 | 実装タスク |
|---|---|
| 対象は葉ジャンル / 子は兄弟追加 / 親は子新設 / `other` は新トップレベル | Task 2（`_leaf_keys`）, Task 4（`other` 分岐）, Task 9（`apply_suggestion` の `parent_id` 決定） |
| `SplitProposal` / `ProposedChild` | Task 1 |
| 戦略 C `demote_generic` | Task 3 |
| 戦略 A `split_own_tags` | Task 2 |
| 戦略 B `promote_free_tags` | Task 4 |
| シミュレーションによる検証・辞書順の罠 | Task 2（`_simulate`）, Task 5（回帰テスト） |
| 成立した案を全部提案し `projected_max` 昇順 | Task 3（planner ループ）, Task 10（一覧の order_by） |
| 定数 4 つ | Task 1（3 つ）, Task 7（`genre_unread_limit`） |
| ラベル生成（LLM 1 回・フォールバック・編集可） | Task 6, Task 8（命名の呼び出し）, Task 9（`labels` 上書き）, Task 13（入力欄） |
| `genre_split_suggestions` テーブル 9 列 | Task 7 |
| 無視は `dismissed_at_count` より増えたら再提案 | Task 8（再提案規則）, Task 9（`dismiss_suggestion`） |
| 無視と適用はジャンル単位 | Task 9（`_close_pending_for_genre`） |
| 4 エンドポイント | Task 10 |
| `reclassify_all` は apply のみ | Task 9（`apply_suggestion` 内だけ）, Task 8（refresh は呼ばない） |
| フィード取得サイクル末尾で検知 | Task 11 |
| フロント（フック / パネル / バッジ） | Task 12, Task 13 |
| テスト一覧（プランナ 6 + API） | Task 1-5（プランナ 8 件）, Task 8-9（ストア 10 件）, Task 10（API 3 件）, Task 11（取得サイクル 2 件） |
| 統合提案は入れない | Task 14（CLAUDE.md に明記） |

漏れなし。

**2. Placeholder scan**

`TBD` / `TODO` / 「適切に処理する」なし。全コードステップに実コードあり。Task 5 Step 3 は「Step 2 が PASS なら不要」と条件を明示した意図的な分岐で、プレースホルダではない。

**3. Type consistency**

- `SplitProposal.before`（dataclass）↔ `GenreSplitSuggestion.before_count`（列）↔ `SplitSuggestionOut.before`（API）— Task 7 で「`before` は SQL 予約語」と理由を明記し、Task 8/10 の変換で一貫。
- `plan_splits(articles, rules, *, limit)` — Task 1 で定義、Task 2/3/4/8 で同じシグネチャ。
- `_plan_*(genre_key, articles, rules, *, limit) -> SplitProposal | None` — 3 プランナすべて同一シグネチャなので Task 3/4 の planner タプルでそのまま回せる。
- `name_genres(tag_groups: list[tuple[str, ...]]) -> list[str]` — Task 6 で定義、Task 8 で `[c.tags for ...]`（`tuple[str, ...]` の列）を渡す。一致。
- `apply_suggestion(...) -> tuple[int, int, int]` — Task 9 で定義、Task 10 で 3 要素にアンパック。一致。
- `estimated_unread` — Task 1/2/4/8/12/13 で同名。
