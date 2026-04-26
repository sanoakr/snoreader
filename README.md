# SnoReader

Innoreader 代替のセルフホスト型 RSS リーダー。LAN / Tailnet 上の複数端末からブラウザでアクセスできる。

## Features

- RSS / Atom フィードの購読・自動取得 (60 分間隔、並列取得)
- 3 ペインレイアウト: フィード一覧 / 記事リスト / 記事リーダー
- 記事の既読・保管管理
- FTS5 による全文検索
- trafilatura による記事本文抽出 (Reader モード)
- タグ付け (手動、将来 AI 自動タグ対応)
- OPML import / export
- キーボードショートカット (`j`/`k` ナビゲーション、`s` 保管、`/` 検索)
- Tailscale HTTPS によるセキュアなリモートアクセス
- ダークモード対応

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.12, FastAPI, SQLAlchemy (async) |
| Frontend | React 19, Vite, TypeScript, Tailwind CSS, TanStack Query |
| Database | SQLite (WAL mode) + FTS5 全文検索 |
| Feed | feedparser, trafilatura |
| Scheduler | APScheduler 3.x |

## Prerequisites

- Python 3.12+
- Node.js 20+
- (本番) Tailscale

## Setup

```bash
# Backend
cd backend
python3 -m venv .venv
source .venv/bin/activate   # fish: source .venv/bin/activate.fish
pip install -e .

# Frontend
cd frontend
npm install
```

## Development

```bash
make dev          # backend (:8000) + frontend (Vite) を同時起動
```

http://localhost:5173 でアクセス。Vite が `/api` リクエストを backend にプロキシする。

## Production (Tailscale HTTPS)

```bash
# 1. Tailscale 証明書を取得 (初回のみ)
make cert HOST=your-host.ts.net

# 2. フロントエンドビルド + HTTPS サーバー起動
make prod
```

`https://your-host.ts.net` でアクセス。uvicorn が API と SPA 静的ファイルを配信する。

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `j` / `Arrow Down` | 次の記事を選択 |
| `k` / `Arrow Up` | 前の記事を選択 |
| `s` | 保管トグル |
| `o` / `Enter` | 元記事をブラウザで開く |
| `/` | 検索にフォーカス |

## Inoreader からの移行

Inoreader の Saved (starred) 記事を SnoReader にインポートできる。

### 方法 1: Inoreader データエクスポート

1. Inoreader で **Preferences > Data management > Export** を開く
2. エクスポートされた JSON (starred.json) をダウンロード
3. SnoReader サイドバーの **Import Saved Articles (JSON)** からアップロード

### 対応フォーマット

- Inoreader / Google Reader 形式 (`{"items": [...]}`)
- 単純な JSON 配列 (`[{"url": "...", "title": "...", ...}]`)

## Project Structure

```
snoreader/
├── backend/
│   └── app/
│       ├── main.py          # FastAPI app + lifespan
│       ├── models.py         # SQLAlchemy ORM models
│       ├── schemas.py        # Pydantic request/response schemas
│       ├── config.py         # Settings (env: SNOREADER_*)
│       ├── database.py       # SQLite async engine
│       ├── routers/          # API endpoints
│       │   ├── feeds.py      #   feed CRUD
│       │   ├── articles.py   #   article list/detail/search/extract
│       │   ├── tags.py       #   tag CRUD + article tagging
│       │   ├── opml.py       #   OPML import/export
│       │   └── imports.py    #   Inoreader/article import
│       ├── services/
│       │   ├── feed_fetcher.py      # RSS fetch + parse
│       │   ├── content_extractor.py # trafilatura article extraction
│       │   └── scheduler.py         # APScheduler periodic fetch
│       └── ai/               # (future) LLM integration
├── frontend/
│   └── src/
│       ├── App.tsx
│       ├── api/client.ts     # API client functions
│       ├── types/index.ts    # TypeScript interfaces
│       ├── hooks/            # TanStack Query hooks
│       └── components/
│           ├── layout/FeedSidebar.tsx
│           └── articles/{ArticleList,ArticleCard,ArticleReader}.tsx
├── data/                     # SQLite DB (gitignored)
├── certs/                    # TLS certificates (gitignored)
└── Makefile
```

## License

Private
