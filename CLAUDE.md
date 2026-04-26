# SnoReader

セルフホスト型 RSS リーダー。LAN/Tailnet 上の複数 PC からブラウザでアクセス。

## 技術スタック

- **Backend**: Python 3.12 + FastAPI + SQLAlchemy (async) + SQLite (WAL + FTS5)
- **Frontend**: React 19 + Vite + TypeScript + Tailwind CSS + TanStack Query
- **Feed**: feedparser, 記事本文抽出: trafilatura
- **スケジューラ**: APScheduler 3.x
- **将来の LLM**: Ternary Bonsai 8B via mlx-lm.server

## 開発コマンド

```bash
# 開発 (backend + frontend 同時起動)
make dev

# 個別起動
make backend   # uvicorn --reload :8000
make frontend  # vite dev server
```

## 本番デプロイ (Tailscale HTTPS)

```bash
# 1. 証明書取得 (初回のみ)
make cert HOST=your-host.ts.net

# 2. ビルド + HTTPS 起動 (port 443)
make prod
```

- `make cert` — `tailscale cert` で Let's Encrypt 証明書を取得、`certs/` に保存
- `make prod` — frontend をビルド → uvicorn が SSL + SPA 静的ファイル配信
- ホスト名は `.host` ファイルに記憶 (git 管理外)

## ディレクトリ構造

- `backend/app/` — FastAPI アプリ (routers/, services/, ai/)
- `frontend/src/` — React SPA (components/, hooks/, api/, types/)
- `data/` — SQLite DB ファイル (git 管理外)
- `certs/` — TLS 証明書 (git 管理外)

## API

### フィード
- `GET /api/feeds` — フィード一覧 (未読数付き)
- `POST /api/feeds` — フィード追加
- `PUT /api/feeds/:id` — フィード更新
- `DELETE /api/feeds/:id` — フィード削除
- `POST /api/feeds/:id/refresh` — フィード手動更新

### 記事
- `GET /api/articles` — 記事一覧 (フィルタ・ソート・ページング)
- `GET /api/articles/:id` — 記事詳細 (タグ付き)
- `PATCH /api/articles/:id` — 記事状態更新 (既読/保管)
- `POST /api/articles/:id/extract` — 記事本文抽出 (trafilatura)
- `POST /api/articles/mark-all-read` — 一括既読

### 検索
- `GET /api/search?q=...` — 全文検索 (FTS5)

### タグ
- `GET /api/tags` — タグ一覧
- `POST /api/tags` — タグ作成
- `DELETE /api/tags/:id` — タグ削除
- `POST /api/articles/:id/tags` — 記事にタグ追加
- `DELETE /api/articles/:id/tags/:tag_id` — 記事からタグ削除

### OPML
- `GET /api/opml/export` — OPML エクスポート
- `POST /api/opml/import` — OPML インポート

### インポート
- `POST /api/import/articles` — Saved 記事インポート (Inoreader/Google Reader JSON 対応)

### AI (要 mlx-lm.server)
- `POST /api/articles/:id/summarize` — AI 要約生成
- `POST /api/articles/:id/suggest-tags` — AI タグ提案
- `GET /api/ai/status` — LLM サーバー接続確認

### 統計
- `GET /api/stats` — フィード別統計

## AI 連携 (mlx-lm.server)

```bash
# LLM サーバー起動 (別ターミナル)
mlx_lm.server --model prism-ml/Ternary-Bonsai-8B-mlx-2bit --port 8880
```

環境変数で設定変更可能:
- `SNOREADER_LLM_BASE_URL` — LLM API URL (default: http://localhost:8880/v1)
- `SNOREADER_LLM_MODEL` — モデル名 (default: default)
- `SNOREADER_LLM_TIMEOUT` — タイムアウト秒 (default: 120)

## キーボードショートカット

- `j` / `↓` — 次の記事
- `k` / `↑` — 前の記事
- `s` — 保管トグル
- `o` / `Enter` — 元記事を開く
- `/` — 検索にフォーカス
