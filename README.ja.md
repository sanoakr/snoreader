# SnoReader

セルフホスト型 RSS リーダー。LAN / Tailnet 上の複数端末からブラウザでアクセスできる。

## 機能

- RSS / Atom フィードの購読・自動取得（60 分間隔、並列取得）
- 3 ペインレイアウト：フィード一覧 / 記事リスト / 記事リーダー
- 記事の既読・保管管理
- SQLite FTS5 による全文検索
- trafilatura による記事本文抽出（Reader モード）
- 日英バイリンガルタグ——日英表示切り替え・手動入力時の自動翻訳
- AI 要約自動生成（バックグラウンドジョブ、日本語箇条書き）
- AI タグ提案（AI 要約から生成）
- OPML インポート / エクスポート
- Saved 記事インポート（Inoreader / Google Reader JSON 形式）
- キーボードショートカット（`j`/`k` ナビ、`s` 保管、`/` 検索）
- ダークモード対応

## 技術スタック

| レイヤー | 技術 |
|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy (async) |
| Frontend | React 19, Vite, TypeScript, Tailwind CSS v4, TanStack Query |
| Database | SQLite（WAL モード）+ FTS5 全文検索 |
| フィード解析 | feedparser, trafilatura |
| スケジューラ | APScheduler 3.x |
| AI（オプション） | mlx-lm.server（ローカル LLM、OpenAI 互換） |

## 前提条件

- Python 3.12+
- Node.js 20+

## セットアップ

```bash
# Backend
cd backend
uv sync          # または: python3 -m venv .venv && source .venv/bin/activate.fish && pip install -e .

# Frontend
cd frontend
npm install
```

## 開発

```bash
make dev          # backend (:8000) + frontend (Vite) を同時起動
```

http://localhost:5173 でアクセス。Vite が `/api` リクエストを backend にプロキシする。

## AI 機能（オプション）

AI 要約・タグ提案にはローカル LLM サーバーが必要。

```bash
# LLM サーバーを起動（別ターミナル）
mlx_lm.server --model prism-ml/Ternary-Bonsai-8B-mlx-2bit --port 8880
```

| 環境変数 | デフォルト | 説明 |
|---|---|---|
| `SNOREADER_LLM_BASE_URL` | `http://localhost:8880/v1` | LLM API の URL |
| `SNOREADER_LLM_MODEL` | `default` | モデル名 |
| `SNOREADER_LLM_TIMEOUT` | `120` | リクエストタイムアウト（秒） |
| `SNOREADER_SUMMARIZE_INTERVAL_SECONDS` | `180` | バックグラウンド要約の実行間隔（秒） |
| `SNOREADER_SUMMARIZE_BATCH_SIZE` | `5` | 1 回の要約バッチ件数 |

LLM サーバーが利用可能な場合、SnoReader は以下を自動実行する：
- 記事の日本語箇条書き要約をバックグラウンドで生成（優先順：Saved > 未読 > 既読）
- AI 要約をもとにタグを提案
- 手動入力された日本語タグを英語に自動翻訳

## 本番デプロイ

```bash
make prod   # フロントエンドビルド + バックエンド起動（ポート 8000）
```

## キーボードショートカット

| キー | 操作 |
|-----|------|
| `j` / `↓` | 次の記事を選択 |
| `k` / `↑` | 前の記事を選択 |
| `s` | 保管トグル |
| `o` / `Enter` | 元記事をブラウザで開く |
| `/` | 検索にフォーカス |

## Inoreader からの移行

Inoreader の Saved（スター付き）記事を SnoReader にインポートできる。

1. Inoreader の **Preferences > Data management > Export** を開く
2. エクスポートされた JSON（`starred.json`）をダウンロード
3. SnoReader サイドバーの **Import Saved Articles (JSON)** からアップロード

対応フォーマット：
- Inoreader / Google Reader 形式：`{"items": [...]}`
- シンプルな JSON 配列：`[{"url": "...", "title": "...", ...}]`

## ディレクトリ構造

```
snoreader/
├── backend/
│   └── app/
│       ├── main.py               # FastAPI アプリ + lifespan
│       ├── models.py             # SQLAlchemy ORM モデル
│       ├── schemas.py            # Pydantic リクエスト/レスポンススキーマ
│       ├── config.py             # 設定（環境変数: SNOREADER_*）
│       ├── database.py           # SQLite 非同期エンジン
│       ├── routers/
│       │   ├── feeds.py          #   フィード CRUD
│       │   ├── articles.py       #   記事一覧/詳細/AI/検索
│       │   ├── tags.py           #   タグ CRUD + 記事タグ付け
│       │   ├── opml.py           #   OPML インポート/エクスポート
│       │   └── imports.py        #   Inoreader/記事インポート
│       ├── services/
│       │   ├── feed_fetcher.py   #   RSS 取得・パース
│       │   ├── content_extractor.py # trafilatura 本文抽出
│       │   └── scheduler.py      #   APScheduler: フィード更新 + AI 要約
│       └── ai/
│           ├── llm_client.py     #   OpenAI 互換 LLM クライアント
│           ├── summarizer.py     #   記事要約
│           └── tagger.py         #   バイリンガルタグ提案
├── frontend/
│   └── src/
│       ├── App.tsx
│       ├── api/client.ts         # API クライアント関数
│       ├── types/index.ts        # TypeScript インターフェース
│       ├── hooks/                # TanStack Query フック
│       └── components/
│           ├── layout/FeedSidebar.tsx
│           └── articles/{ArticleList,ArticleCard,ArticleReader}.tsx
├── data/                         # SQLite DB（git 管理外）
├── certs/                        # TLS 証明書（git 管理外）
└── Makefile
```

## ライセンス

[MIT](LICENSE)
