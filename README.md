# SnoReader

[日本語版 README](README.ja.md)

A self-hosted RSS reader — access from multiple devices on your LAN via browser.

## Features

- RSS / Atom feed subscription with automatic refresh (60-minute interval, parallel fetching)
- 3-pane layout on desktop, single-pane reader with swipe / floating prev-next on mobile
- Mark articles read / unread (toggle) or saved
- Full-text search via SQLite FTS5
- Article content extraction in reader mode (trafilatura, charset-aware for Shift_JIS / EUC-JP sites)
- Stable reader layout — extraction fills in every body image's `width`/`height` (from the page's own markup, falling back to reading the dimensions out of the image header over a ranged request — only for public hosts on ports 80/443, with redirects not followed, since an `<img src>` in fetched page content is attacker-controlled and must not be able to aim requests at loopback or LAN addresses), so the browser reserves the space before the image arrives and the reading position never shifts mid-article. Large GIFs (≥ ~200×200) are not auto-loaded: a placeholder box of exactly the image's own size waits for a tap, which keeps continuous GIF decoding — a cause of full-screen repaint flicker on iOS — off the page. Icon-sized GIFs load normally, and 1×1 beacons / lazy-load spacers (Togetter and Posfie emit dozens per page) are dropped outright once their size is known
- Bilingual tagging — English/Japanese display toggle, manual input with auto-translation
- AI summary auto-generation (background job, Japanese bullet points)
- AI tag suggestions — existing-tag keyword match (title / body, Unicode-safe) merged with LLM candidates
- Keyword auto-tagging is an explicit action, never a side effect of saving. Starring an article attaches nothing on its own — the same keyword matches show up as blue "Suggested:" chips in the reader, one tap each. The sidebar ⚙ menu's `Auto tag` runs the match over Saved articles in bulk: untagged ones get up to 3 tags, and articles with 4 or more have their tags stripped and rebuilt
- Article-scoped LLM chat panel that answers from the article, DuckDuckGo web search, and general knowledge
- Suggested chat questions — tappable chips of short, article-specific questions above the chat input. Pre-generated in the background (unread articles first) and cached per article, so the chips are already there when the article is opened. Sending a chip refreshes the chips into conversation-aware follow-ups; typing your own question leaves them as they are
- IDF-weighted "Recommend" view with automatic exclusion of high-coverage tags (present on over 20% of saved articles — such a tag says nothing about what you prefer). An article needs at least **two** surviving tags in common with your saved articles — a single chance overlap does not qualify it. The requirement is a tag count rather than a score floor because the score range grows with the size of the saved corpus, so any absolute floor stops discriminating as saved articles accumulate (sidebar order: All / Recommend / Saved)
- Genre triage for the unread backlog — every article is assigned exactly one genre by a deterministic tag→genre dictionary (no LLM, so a genre is a complete set you can act on in bulk). The sidebar lists genres with unread counts; picking one enables **まとめて既読** and **まとめて非表示** for the whole genre, with a confirmation and an inline undo
- Subgenres — a genre can hold one level of children. When a parent's unread count goes over 30, the sidebar expands its children so no single bucket looks too big to clear; below the threshold it stays a single row (and stays expanded while you are reading inside that parent, so the row you picked does not vanish under you). A tag rule points at whichever level owns it, and resolution prefers the more specific one, so moving a genre's dominant tag down to a child actually splits the bucket. Selecting a parent covers its children too; the synthetic "その他" row selects only what is classified directly on the parent. A bucket still over 30 gets an amber badge, since a skewed tag distribution cannot always be split by meaning. `ジャンル管理` has a `推奨サブジャンルを投入` button that installs a measured default split for AI・LLM and 開発・技術 (idempotent; re-running it only adds what is missing)
- Hidden (dismissed) articles — hiding never marks an article read, so hidden articles stay out of the retention-based auto-delete and can be restored from the "非表示にした記事" view or from search, where they carry a 非表示 badge
- Editable genre dictionary — add/remove genres, rename them, reorder priority (which genre wins when an article's tags hit several), and move tags between genres from the sidebar's ジャンル管理 panel. Every change reclassifies existing articles immediately and reports how many moved; unclassified articles are reachable as the "その他" genre so the dictionary can be grown from real data
- Saved view with per-tag filter chips plus an "Untagged" chip
- Related saved articles at the bottom of the reader — 3 random Saved articles that share at least one tag (or AI tag suggestion when the current article has no manual tags); clicking one loads it into the right pane
- Extract-failure management UI — articles whose body fetch failed are classified (404 / 403 / transient error / user-skipped) and surfaced in a sidebar modal with per-article **retry / summary-only / delete** actions, plus bulk ops. Transient errors auto-retry after a 5-minute backoff; permanent failures stop blocking LLM summarization so Phase 1 falls back to the RSS summary
- OPML import / export
- Saved articles import (Inoreader / Google Reader JSON format)
- Cross-feed duplicate cleanup — matches articles by normalized URL (tracking params/fragment stripped), keeping saved > non-Hatena-Bookmark > oldest fetched, and merges read/saved state and tags from the removed copy. Runs automatically after each scheduled fetch, plus an on-demand "重複記事を整理" button in the sidebar with a dry-run preview
- Keyboard shortcuts (`j`/`k` navigation, `s` save, `/` search)
- Mobile top bar shows the current category, view total, and overall unread count
- Dark mode support

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.12, FastAPI, SQLAlchemy (async) |
| Frontend | React 19, Vite, TypeScript, Tailwind CSS v4, TanStack Query |
| Database | SQLite (WAL mode) + FTS5 full-text search |
| Feed parsing | feedparser, trafilatura |
| Scheduler | APScheduler 3.x |
| AI (optional) | mlx-lm.server (local LLM, OpenAI-compatible) |
| Web search (optional) | DuckDuckGo via `ddgs` |

## Prerequisites

- Python 3.12+
- Node.js 20+

## Setup

```bash
# Backend
cd backend
uv sync          # or: python3 -m venv .venv && source .venv/bin/activate && pip install -e .

# Frontend
cd frontend
npm install
```

## Development

```bash
make dev          # starts backend (:8000) + frontend (Vite) concurrently
```

Open http://localhost:5173. Vite proxies `/api` requests to the backend.

## AI Features (optional)

AI summary and tag suggestion require a local LLM server.

```bash
# Start the LLM server (separate terminal)
mlx_lm.server --model prism-ml/Ternary-Bonsai-8B-mlx-2bit --port 8880
```

| Environment variable | Default | Description |
|---|---|---|
| `SNOREADER_LLM_BASE_URL` | `http://localhost:8880/v1` | LLM API base URL |
| `SNOREADER_LLM_MODEL` | `default` | Model name |
| `SNOREADER_LLM_TIMEOUT` | `120` | Request timeout (seconds) |
| `SNOREADER_LLM_REASONING_EFFORT` | `none` | Reasoning (thinking) effort. `none` disables thinking, which makes summarization 4-5x faster at no quality cost. Set to an empty string for servers that do not accept the parameter. Note that `none` also turns off the server's thinking *parser*, so a model that thinks anyway leaks its reasoning into the reply body — SnoReader strips that in `llm_client` (see below). |
| `SNOREADER_SUMMARIZE_INTERVAL_SECONDS` | `180` | Background summarization interval |
| `SNOREADER_SUMMARIZE_BATCH_SIZE` | `5` | Articles per summarization batch |

When the LLM server is available, SnoReader:
- Auto-generates Japanese bullet-point summaries for articles (background job, priority: Saved > Unread > Read)
- Suggests tags based on the AI summary
- Auto-translates manually entered Japanese tags into English
- Enables a chat panel at the bottom of the reader pane for free-form questions about the current article (session-only history, cleared on article switch)

### Thinking-block stripping

`llm_client.chat_completion` drops a thinking block the server left in the reply body before returning it, and returns nothing when the reply was cut off mid-thinking.

This is needed because `SNOREADER_LLM_REASONING_EFFORT=none` tells Ollama not to *parse* thinking, not merely not to request it. When the model thinks anyway — measured at 11 of 24 replies for `qwen3.8:27b-mlx` on a long article with chat history — the reasoning text and its closing `</think>` land in the message body. The opening tag is injected by the chat template rather than generated, so the shape is `<draft>…</think><answer>` with no `<think>` at all, which is why the split is on the closing tag. Summaries and tags never showed this because `finalize_bullets` and the tag regex discard anything unstructured; chat and question suggestions pass the text straight through.

### Runaway repetition

The model occasionally answers correctly and then repeats itself verbatim until it exhausts `max_tokens` (observed at 10001 characters with `finish_reason: "length"`), in two shapes: a single sentence repeating, and a two-paragraph A/B/A/B cycle. Two guards:

- The chat endpoint sends `frequency_penalty` (0.5). It is passed per call rather than set globally, because summaries repeat `・` and tag output repeats `|` and `,` by design — penalising repeated tokens everywhere would damage those structured formats.
- `llm_client` cuts a repeating cycle back to one occurrence before returning. The loop always runs to the end of the message (that is what exhausts the budget), so the repetition is always a suffix and cutting it is safe. Blank lines are ignored when matching, since the observed loops put one between each repeat.

The penalty makes the loop rarer; the cut makes it invisible when it still happens.

### Suggested chat questions

Above the chat input, SnoReader shows up to 4 short question chips tailored to the current article; clicking one sends it as the next message.

Suggestions are cached in `articles.chat_suggestions` and served by `GET /api/articles/{id}/chat-suggestions`. That endpoint never calls the LLM by default — opening an article only reads the cache. The cache is filled ahead of time by the background processor's **Phase 3**, which walks every summarized article and generates its chips, so in normal use the chips are simply there when the panel opens. Phase 3 runs on the `bulk` lane at `PRIORITY_IDLE`, the lowest priority in the queue, so summary and tag generation (`PRIORITY_BACKGROUND`) always drains first and foreground requests on the `reserved` lane are untouched. Its order is unread → saved → newest, because chat is opened on articles you have not read yet. The sidebar's AI status line shows the remaining count as `質問候補 N件`.

When nothing is cached yet — a brand-new article, or one where the model returned nothing usable — the panel falls back to a **✨ 質問候補を生成** button, and that click issues the request with `?generate=true`. Generation takes roughly 8 seconds against a warm local model and is stored, so the chips are instant on every later visit.

The chips stay visible for the whole conversation. Sending one calls `POST /api/articles/{id}/chat-suggestions` with the conversation so far and replaces the chips with follow-ups that build on the answer instead of repeating it; the old chips stay on screen with a small spinner until the new ones arrive. Typing your own question does **not** trigger that call — the current chips simply remain. Follow-ups are never written to `chat_suggestions`, so one reader's chat session can't change the opening questions the next visit shows.

### Chat web search

The article is the chat's primary context, but the assistant may also answer from web search results and its own general knowledge, marking anything that comes from outside the article as such.

A DuckDuckGo search via `ddgs` runs when the message contains an explicit search instruction (`検索`, `調べて`, `search`), a recency keyword (`最新`, `latest`, or a "今…？" question), or an explanation request (`とは`, `意味`, `背景`, `経緯`, `違い`, `なぜ`, `理由`, `what is`, `why`, `explain`, …). Requests that only restate the article (`要約`, `まとめて`, `結論`) skip the search to avoid the added latency, unless an explicit search instruction is also present. The top 3 results are injected into the LLM context and their source links are returned alongside the reply. Search failures or timeouts fall back silently to article-and-knowledge answers.

## Production

```bash
make prod   # build frontend + start backend on port 8000
```

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `j` / `Arrow Down` | Next article |
| `k` / `Arrow Up` | Previous article |
| `s` | Toggle save |
| `o` / `Enter` | Open original article in browser |
| `/` | Focus search |
| `r` | Refetch articles and feeds |

## Migrating from Inoreader

You can import your Inoreader Saved (starred) articles into SnoReader.

1. In Inoreader, go to **Preferences > Data management > Export**
2. Download the exported JSON (`starred.json`)
3. In the SnoReader sidebar, click **Import Saved Articles (JSON)** and upload the file

Supported formats:
- Inoreader / Google Reader format: `{"items": [...]}`
- Plain JSON array: `[{"url": "...", "title": "...", ...}]`

## Project Structure

```
snoreader/
├── backend/
│   └── app/
│       ├── main.py               # FastAPI app + lifespan
│       ├── models.py             # SQLAlchemy ORM models
│       ├── schemas.py            # Pydantic request/response schemas
│       ├── config.py             # Settings (env: SNOREADER_*)
│       ├── database.py           # SQLite async engine
│       ├── routers/
│       │   ├── feeds.py          #   feed CRUD
│       │   ├── articles.py       #   article list/detail/AI/search
│       │   ├── tags.py           #   tag CRUD + article tagging
│       │   ├── opml.py           #   OPML import/export
│       │   └── imports.py        #   Inoreader/article import
│       ├── services/
│       │   ├── feed_fetcher.py   #   RSS fetch + parse
│       │   ├── content_extractor.py # trafilatura article extraction
│       │   ├── scheduler.py      #   APScheduler: feed refresh + AI summarization
│       │   └── web_search.py     #   DuckDuckGo search helper for chat
│       └── ai/
│           ├── llm_client.py     #   OpenAI-compatible LLM client
│           ├── summarizer.py     #   article summarization
│           ├── question_suggester.py # chat question suggestions
│           └── tagger.py         #   bilingual tag suggestion
├── frontend/
│   └── src/
│       ├── App.tsx
│       ├── api/client.ts         # API client functions
│       ├── types/index.ts        # TypeScript interfaces
│       ├── hooks/                # TanStack Query hooks
│       └── components/
│           ├── layout/FeedSidebar.tsx
│           └── articles/{ArticleList,ArticleCard,ArticleReader,ArticleChatPanel}.tsx
├── data/                         # SQLite DB (gitignored)
├── certs/                        # TLS certificates (gitignored)
└── Makefile
```

## License

[MIT](LICENSE)
