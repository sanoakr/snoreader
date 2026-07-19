# AIサマリー改善 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the AI summary from repeating title information, make it include the article's conclusion/result, and let its bullet count flex from 1 to 9 (fewer is better) instead of a fixed 3 — then regenerate all existing summaries under the new rules.

**Architecture:** Extract the summary rule text and bullet-list post-processing (`finalize_bullets`) that are currently duplicated between `app/ai/summarizer.py` and `app/ai/processor.py` into a new shared module `app/ai/summary_rules.py`. Both callers keep their own prompt structure and SUMMARY/TAGS parsing, but delegate the "clean up the bullet list" step to the shared function. A new admin endpoint clears `ai_summary` on all articles so the existing background processor (Phase 1) regenerates them under the new prompt — no new batch machinery needed.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy (async), pytest + pytest-asyncio, httpx `AsyncClient` for endpoint tests.

## Global Constraints

- Bullet count: minimum 1, maximum 9 (`MIN_SUMMARY_BULLETS` / `MAX_SUMMARY_BULLETS` in `summary_rules.py`), prefer as few as possible.
- Summary bullets must not restate information already present in the article title.
- Summary must include the article's conclusion/result.
- Summary bullets: Japanese only, each line starts with `・`, no `word|word`-style tag annotations mixed in.
- No frontend changes. `regenerate-summaries` is a backend-only admin endpoint invoked manually (same pattern as the existing `regenerate-tag-suggestions`).
- Regenerating summaries via the new endpoint is expected to also refresh `tag_suggestions` for affected articles (accepted side effect of the combined `summarize_and_tag` call) — do not add logic to preserve old tags.

---

### Task 1: Shared summary rules module

**Files:**
- Create: `backend/app/ai/summary_rules.py`
- Test: `backend/tests/test_summary_rules.py`

**Interfaces:**
- Produces: `MIN_SUMMARY_BULLETS: int = 1`, `MAX_SUMMARY_BULLETS: int = 9`, `SUMMARY_RULES: str` (a `"- ..."`-bulleted, `\n`-joined block of English rule sentences meant to be embedded under a prompt's `"Rules:\n"` header), `finalize_bullets(lines: list[str]) -> str | None`.

- [ ] **Step 1: Set up the backend dev environment (if not already done in this worktree)**

Run:
```bash
cd backend && uv sync --extra dev
```
Expected: completes without error, creates `backend/.venv` with `pytest` and `pytest-asyncio` installed.

- [ ] **Step 2: Write the failing test**

Create `backend/tests/test_summary_rules.py`:

```python
"""Tests for shared AI summary bullet post-processing."""

from app.ai.summary_rules import MAX_SUMMARY_BULLETS, finalize_bullets


def test_passes_through_normal_bullets():
    lines = ["・第一の事実", "・第二の事実"]
    assert finalize_bullets(lines) == "・第一の事実\n・第二の事実"


def test_single_bullet_allowed():
    assert finalize_bullets(["・唯一の事実"]) == "・唯一の事実"


def test_caps_at_max_bullets():
    lines = [f"・項目{i}" for i in range(MAX_SUMMARY_BULLETS + 5)]
    result = finalize_bullets(lines)
    assert result is not None
    assert result.splitlines() == lines[:MAX_SUMMARY_BULLETS]


def test_drops_tag_annotated_lines():
    lines = ["・正常な要約", "・security|セキュリティ", "・もう一つの事実"]
    assert finalize_bullets(lines) == "・正常な要約\n・もう一つの事実"


def test_empty_input_returns_none():
    assert finalize_bullets([]) is None


def test_all_lines_rejected_returns_none():
    assert finalize_bullets(["・ai|エーアイ"]) is None
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd backend && uv run pytest tests/test_summary_rules.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ai.summary_rules'`

- [ ] **Step 4: Implement `summary_rules.py`**

Create `backend/app/ai/summary_rules.py`:

```python
"""Shared rules and bullet-list post-processing for AI-generated article summaries."""

from __future__ import annotations

import re

MIN_SUMMARY_BULLETS = 1
MAX_SUMMARY_BULLETS = 9

SUMMARY_RULES = (
    f"- SUMMARY: {MIN_SUMMARY_BULLETS}-{MAX_SUMMARY_BULLETS} Japanese bullet points — "
    "use as FEW as possible, only add a bullet if it conveys genuinely new information\n"
    "- Do NOT restate information already given in the title — every bullet must add "
    "something the title doesn't already say\n"
    "- Always include the article's conclusion, result, or outcome, so the title and "
    "summary together give a complete understanding of the article\n"
    "- SUMMARY bullets start with '・' and contain ONLY Japanese text — no English|Japanese pairs\n"
    "- Focus on key facts and takeaways only. Do not add opinions."
)

# Rejects summary bullets containing tag-format annotations like "security|セキュリティ"
_TAG_IN_BULLET = re.compile(r"\b[a-z]{2,}\|")


def finalize_bullets(lines: list[str]) -> str | None:
    """Drop tag-annotated lines, cap at MAX_SUMMARY_BULLETS, and join.

    `lines` must already be stripped, ・-prefixed bullet lines. Returns None
    if no valid bullet lines remain (treated as an LLM formatting failure).
    """
    cleaned = [line for line in lines if not _TAG_IN_BULLET.search(line)]
    cleaned = cleaned[:MAX_SUMMARY_BULLETS]
    return "\n".join(cleaned) if cleaned else None
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd backend && uv run pytest tests/test_summary_rules.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
git add backend/app/ai/summary_rules.py backend/tests/test_summary_rules.py
git commit -m "feat: add shared summary bullet rules and post-processing"
```

---

### Task 2: Refactor `summarizer.py` to use the shared rules

**Files:**
- Modify: `backend/app/ai/summarizer.py` (whole file, currently 54 lines)
- Test: `backend/tests/test_summarizer.py`

**Interfaces:**
- Consumes: `app.ai.summary_rules.SUMMARY_RULES: str`, `app.ai.summary_rules.finalize_bullets(lines: list[str]) -> str | None`, `app.ai.summary_rules.MAX_SUMMARY_BULLETS: int` (Task 1).
- Produces: `summarize_article(title: str, text: str, priority: int | None = None) -> str | None` (signature unchanged), `_clean_summary(raw: str) -> str | None` (unchanged signature, new behavior).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_summarizer.py`:

```python
"""Tests for summarizer._clean_summary."""

from app.ai.summarizer import _clean_summary
from app.ai.summary_rules import MAX_SUMMARY_BULLETS


def test_clean_summary_keeps_single_bullet():
    assert _clean_summary("・唯一の結論") == "・唯一の結論"


def test_clean_summary_keeps_up_to_nine_bullets():
    raw = "\n".join(f"・項目{i}" for i in range(MAX_SUMMARY_BULLETS))
    result = _clean_summary(raw)
    assert result is not None
    assert result.count("\n") + 1 == MAX_SUMMARY_BULLETS


def test_clean_summary_caps_beyond_nine():
    raw = "\n".join(f"・項目{i}" for i in range(MAX_SUMMARY_BULLETS + 3))
    result = _clean_summary(raw)
    assert result is not None
    assert result.count("\n") + 1 == MAX_SUMMARY_BULLETS


def test_clean_summary_ignores_non_bullet_lines():
    raw = "前置き\n・唯一の結論\n後書き"
    assert _clean_summary(raw) == "・唯一の結論"


def test_clean_summary_returns_none_when_no_bullets():
    assert _clean_summary("no bullets here") is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd backend && uv run pytest tests/test_summarizer.py -v`
Expected: FAIL — `test_clean_summary_keeps_up_to_nine_bullets` and `test_clean_summary_caps_beyond_nine` fail (current code caps at 3, so `result.count("\n") + 1 == 3`, not 9). The other 3 tests already pass unmodified.

- [ ] **Step 3: Implement the refactor**

Replace the full contents of `backend/app/ai/summarizer.py`:

```python
"""Article summarization using local LLM."""

from __future__ import annotations

import hashlib
import logging

from app.ai import summary_rules
from app.ai.llm_client import chat_completion

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a concise article summarizer. "
    "ALWAYS respond in Japanese, regardless of the article's language.\n"
    "Rules:\n"
    f"{summary_rules.SUMMARY_RULES}\n"
    "- Do NOT output any English tags, labels, or 'word|translation' pairs\n"
    "- Do NOT output section headers like 'SUMMARY:' or 'TAGS:'\n"
    "Output ONLY the Japanese bullet points starting with '・', nothing else."
)


def _clean_summary(raw: str) -> str | None:
    """Extract ・-prefixed lines from LLM output and finalize them."""
    lines = [
        line.strip()
        for line in raw.splitlines()
        if line.strip().startswith("・")
    ]
    return summary_rules.finalize_bullets(lines)


async def summarize_article(title: str, text: str, priority: int | None = None) -> str | None:
    """Generate a summary for an article. Returns None if LLM is unavailable."""
    content = text[:3000]
    # Unique per-article hash prefix prevents mlx-lm KV cache reuse across articles
    uid = hashlib.md5(f"sum:{title}".encode()).hexdigest()[:8]
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"[{uid}] Summarize only this article.\n\n"
                f"Title: {title}\n\n{content}"
            ),
        },
    ]
    raw = await chat_completion(messages, max_tokens=1536, temperature=0.2, priority=priority)
    if not raw:
        return None
    return _clean_summary(raw)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd backend && uv run pytest tests/test_summarizer.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/ai/summarizer.py backend/tests/test_summarizer.py
git commit -m "feat: let summarizer use flexible 1-9 bullet summary rules"
```

---

### Task 3: Refactor `processor.py` to use the shared rules

**Files:**
- Modify: `backend/app/ai/processor.py` (whole file, currently 106 lines)
- Test: `backend/tests/test_processor.py`

**Interfaces:**
- Consumes: `app.ai.summary_rules.SUMMARY_RULES: str`, `app.ai.summary_rules.finalize_bullets(lines: list[str]) -> str | None`, `app.ai.summary_rules.MAX_SUMMARY_BULLETS: int` (Task 1).
- Produces: `summarize_and_tag(title, text, existing_tags=None, priority=None) -> tuple[str | None, list[tuple[str, str | None]]]` (signature unchanged), `_parse_output(raw: str) -> tuple[str | None, list[tuple[str, str | None]]]` (unchanged signature, new behavior).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_processor.py`:

```python
"""Tests for processor._parse_output."""

from app.ai.processor import _parse_output
from app.ai.summary_rules import MAX_SUMMARY_BULLETS


def test_parse_output_single_bullet_and_tags():
    raw = (
        "SUMMARY:\n"
        "・唯一の結論\n"
        "TAGS: python|Python, security|セキュリティ"
    )
    summary, pairs = _parse_output(raw)
    assert summary == "・唯一の結論"
    assert pairs == [("python", "Python"), ("security", "セキュリティ")]


def test_parse_output_keeps_up_to_nine_bullets():
    bullets = "\n".join(f"・項目{i}" for i in range(MAX_SUMMARY_BULLETS + 4))
    raw = f"SUMMARY:\n{bullets}\nTAGS: news|ニュース"
    summary, _ = _parse_output(raw)
    assert summary is not None
    assert summary.count("\n") + 1 == MAX_SUMMARY_BULLETS


def test_parse_output_drops_tag_annotated_bullet():
    raw = (
        "SUMMARY:\n"
        "・正常な要約\n"
        "・security|セキュリティ\n"
        "TAGS: security|セキュリティ"
    )
    summary, pairs = _parse_output(raw)
    assert summary == "・正常な要約"
    assert pairs == [("security", "セキュリティ")]


def test_parse_output_no_valid_bullets_returns_none_summary():
    raw = "SUMMARY:\nTAGS: news|ニュース"
    summary, pairs = _parse_output(raw)
    assert summary is None
    assert pairs == [("news", "ニュース")]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd backend && uv run pytest tests/test_processor.py -v`
Expected: FAIL — `test_parse_output_keeps_up_to_nine_bullets` fails (current code caps at 3 via `summary_lines[:3]`). The other 3 tests already pass unmodified.

- [ ] **Step 3: Implement the refactor**

Replace the full contents of `backend/app/ai/processor.py`:

```python
"""Combined article processor: summary + tag suggestions in a single LLM call.

Using a single call is required for the Gemma 4 model, which can only
generate tag pairs reliably when it continues from a SUMMARY section it just wrote.
"""

from __future__ import annotations

import hashlib
import logging
import re

from app.ai import summary_rules
from app.ai.llm_client import chat_completion

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are an article processor. Output a summary and topic tags for the given article.\n"
    "Output format (follow EXACTLY — no extra text):\n"
    "SUMMARY:\n"
    "・<bullet point in Japanese>\n"
    "TAGS: <english>|<日本語>, <english>|<日本語>\n\n"
    "Rules:\n"
    f"{summary_rules.SUMMARY_RULES}\n"
    "- TAGS: 1-3 specific topic tags; single lowercase English word (or hyphenated) + Japanese translation\n"
    "- Choose the most specific accurate tag — only use 'ai', 'technology', or 'news' if that is the article's primary subject\n"
    "- If existing tags are provided, reuse them when appropriate\n"
    "- Return ONLY the formatted block above, nothing else"
)

# Valid English tag: starts with a-z, contains only a-z/0-9/hyphen, 1-29 total chars
_VALID_EN_TAG = re.compile(r"^[a-z][a-z0-9-]{0,28}$")


def _parse_output(raw: str) -> tuple[str | None, list[tuple[str, str | None]]]:
    """Parse combined LLM output into (summary, [(en, ja), ...])."""
    summary_lines: list[str] = []
    tags_str = ""
    in_summary = False

    for line in raw.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith("SUMMARY:"):
            in_summary = True
        elif upper.startswith("TAGS:"):
            in_summary = False
            tags_str = stripped[5:].strip()
        elif in_summary and stripped.startswith("・"):
            summary_lines.append(stripped)

    summary = summary_rules.finalize_bullets(summary_lines)

    pairs: list[tuple[str, str | None]] = []
    for item in re.split(r"[,\n]", tags_str):
        item = item.strip().strip("\"'")
        if not item:
            continue
        if "|" in item:
            en, ja = item.split("|", 1)
            en = en.strip().lower()
            ja = ja.strip() or None
        else:
            en = item.lower().strip()
            ja = None
        if _VALID_EN_TAG.match(en):
            pairs.append((en, ja))
    pairs = pairs[:3]

    return summary, pairs


async def summarize_and_tag(
    title: str,
    text: str,
    existing_tags: list[str] | None = None,
    priority: int | None = None,
) -> tuple[str | None, list[tuple[str, str | None]]]:
    """Generate summary and tag suggestions in a single LLM call.

    Returns (summary_text | None, [(en, ja), ...]).
    """
    existing_str = f"\nExisting tags: {', '.join(existing_tags)}" if existing_tags else ""
    # Per-article hash prefix breaks mlx-lm KV cache chain between consecutive articles
    uid = hashlib.md5(f"proc:{title}".encode()).hexdigest()[:8]
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"[{uid}] Process only this article."
                f"{existing_str}\n\nTitle: {title}\n\n{text[:3000]}"
            ),
        },
    ]
    result = await chat_completion(messages, max_tokens=1792, temperature=0.2, priority=priority)
    if not result:
        return None, []
    return _parse_output(result)
```

Note: this removes the module-level `_TAG_IN_BULLET` regex from `processor.py` (now lives only in `summary_rules.py`) and drops the inline tag-annotation check from the parsing loop — `finalize_bullets` does that once, at the end.

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd backend && uv run pytest tests/test_processor.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/ai/processor.py backend/tests/test_processor.py
git commit -m "feat: let processor use flexible 1-9 bullet summary rules"
```

---

### Task 4: Bulk-regenerate existing AI summaries

**Files:**
- Modify: `backend/app/routers/articles.py` — add new endpoint directly after `regenerate_tag_suggestions` (currently ends at line 822)
- Test: `backend/tests/test_regenerate_summaries.py`

**Interfaces:**
- Consumes: `Article.ai_summary` column (`backend/app/models.py:61`), existing `regenerate_tag_suggestions` endpoint as the pattern to mirror (`backend/app/routers/articles.py:807-822`).
- Produces: `POST /api/articles/regenerate-summaries` → `{"cleared": int}`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_regenerate_summaries.py`:

```python
"""Tests for POST /api/articles/regenerate-summaries."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update


@pytest_asyncio.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SNOREADER_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    # config / database / main は環境変数を読み込んだあとに import する必要がある
    import importlib

    from app import config as config_module

    config_module.settings = config_module.Settings()  # type: ignore[assignment]

    from app import database as database_module

    importlib.reload(database_module)

    from app import main as main_module

    importlib.reload(main_module)

    from app.database import async_session
    from app.models import Article, Feed

    async with main_module.lifespan(main_module.app):
        async with async_session() as session:
            feed = Feed(url="https://example.com/feed", title="Example")
            session.add(feed)
            await session.flush()
            session.add_all(
                [
                    Article(
                        feed_id=feed.id,
                        guid="a1",
                        url="https://example.com/1",
                        title="要約済み記事",
                        summary="元の本文",
                        ai_summary="・既存の要約",
                        tag_suggestions='["existing"]',
                    ),
                    Article(
                        feed_id=feed.id,
                        guid="a2",
                        url="https://example.com/2",
                        title="未要約の記事",
                        summary="元の本文2",
                    ),
                ]
            )
            await session.commit()

        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.mark.asyncio
async def test_regenerate_summaries_clears_only_existing(client: AsyncClient) -> None:
    """ai_summary が設定済みの記事だけを NULL に戻し、件数を返すこと。"""
    res = await client.post("/api/articles/regenerate-summaries")
    assert res.status_code == 200
    assert res.json() == {"cleared": 1}

    from app.database import async_session
    from app.models import Article

    async with async_session() as session:
        articles = (await session.execute(select(Article))).scalars().all()
        by_guid = {a.guid: a for a in articles}
        assert by_guid["a1"].ai_summary is None
        assert by_guid["a2"].ai_summary is None  # already None, unaffected


@pytest.mark.asyncio
async def test_regenerate_summaries_no_articles_returns_zero(client: AsyncClient) -> None:
    """ai_summary が1件も設定されていない場合は cleared: 0 を返すこと。"""
    from app.database import async_session
    from app.models import Article

    async with async_session() as session:
        await session.execute(update(Article).values(ai_summary=None))
        await session.commit()

    res = await client.post("/api/articles/regenerate-summaries")
    assert res.status_code == 200
    assert res.json() == {"cleared": 0}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd backend && uv run pytest tests/test_regenerate_summaries.py -v`
Expected: FAIL — `404 Not Found` (route doesn't exist yet), both tests fail on `assert res.status_code == 200`.

- [ ] **Step 3: Implement the endpoint**

In `backend/app/routers/articles.py`, immediately after the existing `regenerate_tag_suggestions` function (ends around line 822, right before the `@router.get("/ai/status")` handler), add:

```python
@router.post("/articles/regenerate-summaries", response_model=dict)
async def regenerate_summaries(
    session: AsyncSession = Depends(get_session),
):
    """既存の ai_summary を NULL に戻し、background processor の Phase 1 に再生成させる。

    AIサマリー改善（タイトル重複排除・結論必須・1〜9項目化）のプロンプト変更を
    既存記事にも反映するための管理用エンドポイント。
    """
    result = await session.execute(
        update(Article)
        .where(Article.ai_summary.isnot(None))
        .values(ai_summary=None)
    )
    await session.commit()
    return {"cleared": result.rowcount or 0}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd backend && uv run pytest tests/test_regenerate_summaries.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the full backend test suite to check for regressions**

Run: `cd backend && uv run pytest -v`
Expected: PASS, all tests (existing + new) green.

- [ ] **Step 6: Update API documentation in project CLAUDE.md**

In `/Users/sano/snoreader/CLAUDE.md`, under the `### 記事` section (after the `POST /api/articles/mark-all-read` line, following the existing `dedup` entry if present), add:

```
- `POST /api/articles/regenerate-summaries` — 既存 AI サマリーを一括クリアし、背景処理での再生成をトリガー
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/routers/articles.py backend/tests/test_regenerate_summaries.py CLAUDE.md
git commit -m "feat: add endpoint to bulk-regenerate AI summaries under new rules"
```

---

## Out of scope (deliberately not covered by this plan)

- **Version bump + git tag**: per project convention, `backend/pyproject.toml` / `frontend/package.json` version and the corresponding git tag are bumped when this branch is committed-and-pushed for merge, not per implementation task. Handle this as part of finishing the branch (`superpowers:finishing-a-development-branch`), checking `git tag -l` / `origin/main` for the latest version at that time rather than assuming one now.
- **Production deploy** (`make deploy` + `launchctl kickstart -k`): only relevant once this branch is merged into `main` in the primary checkout — this plan's worktree is not the production checkout.
- **Actually invoking `POST /api/articles/regenerate-summaries` in production**: this plan only adds the endpoint; triggering it against the real database is a manual post-deploy step for the user to run once satisfied with the new prompt's output quality.
