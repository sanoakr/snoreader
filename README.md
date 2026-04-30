# SnoReader

[日本語版 README](README.ja.md)

A self-hosted RSS reader — access from multiple devices on your LAN via browser.

## Features

- RSS / Atom feed subscription with automatic refresh (60-minute interval, parallel fetching)
- 3-pane layout: feed list / article list / article reader
- Mark articles as read or saved
- Full-text search via SQLite FTS5
- Article content extraction in reader mode (trafilatura)
- Bilingual tagging — English/Japanese display toggle, manual input with auto-translation
- AI summary auto-generation (background job, Japanese bullet points)
- AI tag suggestions (generated from AI summary)
- Article-scoped LLM chat panel with optional DuckDuckGo web search (triggered by keywords like "検索", "最新", "調べて")
- IDF-weighted "Recommend" view with automatic exclusion of high-coverage tags
- OPML import / export
- Saved articles import (Inoreader / Google Reader JSON format)
- Keyboard shortcuts (`j`/`k` navigation, `s` save, `/` search)
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
| `SNOREADER_SUMMARIZE_INTERVAL_SECONDS` | `180` | Background summarization interval |
| `SNOREADER_SUMMARIZE_BATCH_SIZE` | `5` | Articles per summarization batch |

When the LLM server is available, SnoReader:
- Auto-generates Japanese bullet-point summaries for articles (background job, priority: Saved > Unread > Read)
- Suggests tags based on the AI summary
- Auto-translates manually entered Japanese tags into English
- Enables a chat panel at the bottom of the reader pane for free-form questions about the current article (session-only history, cleared on article switch)

### Chat web search

When a chat message contains a trigger word (`検索`, `調べて`, `search`, `最新`, `latest`, or a "今…？" question), the backend runs a DuckDuckGo search via `ddgs`, injects the top 3 results into the LLM context, and returns source links alongside the reply. Search failures or timeouts fall back silently to article-only answers.

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
