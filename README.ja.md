# SnoReader

セルフホスト型 RSS リーダー。LAN 上の複数端末からブラウザでアクセスできる。

## 機能

- RSS / Atom フィードの購読・自動取得（60 分間隔、並列取得）
- デスクトップは 3 ペインレイアウト、モバイルは単ペインリーダー（スワイプ / フローティング前後ボタンで記事移動）
- 記事の既読・未読トグル、保管管理
- SQLite FTS5 による全文検索
- trafilatura による記事本文抽出（Reader モード、Shift_JIS / EUC-JP サイトにも charset 対応）
- 日英バイリンガルタグ——日英表示切り替え・手動入力時の自動翻訳
- AI 要約自動生成（バックグラウンドジョブ、日本語箇条書き）
- AI タグ提案——既存タグとのキーワードマッチ（タイトル / 本文、Unicode セーフ）と LLM 候補をマージ
- 保管時の自動タグ付け——未タグ記事をスターすると一致する既存タグを自動付与（1 記事あたり最大 3 件）。サイドバー ⚙ メニューの `Auto tag` では 4 件以上タグが付いた Saved 記事も既存タグを剥がして再付与する
- 記事単位の LLM チャットパネル（記事本文・DuckDuckGo Web 検索・一般知識を併用して回答）
- IDF 重み付き「Recommend」ビュー（カバー率 30% 以上のタグを自動除外、弱い単一タグ一致を抑制するスコア下限を設定）
- 「Unrecommend」ビュー——保存済みタグとの重複がゼロの未読記事（サイドバー順: All / Recommend / Unrecommend / Saved）
- 未読のジャンル別トリアージ——タグ→ジャンルの決定的な辞書（LLM を使わない）で全記事を 1 つのジャンルに割り当てる。LLM は記事をまたぐと語彙が揺れて束が歯抜けになるため、一括操作の対象を「漏れのない集合」にするのが辞書方式の理由。サイドバーにジャンルと未読件数が並び、選ぶと**まとめて既読**・**まとめて非表示**が使える（確認ダイアログとその場の取り消し付き）
- 非表示（dismissed）——非表示は既読化ではないので、保持期間による自動削除の対象にならない。「非表示にした記事」ビューや検索から戻せる（検索結果では 非表示 バッジが付く）
- ジャンル辞書の編集——サイドバーの「ジャンル管理」から、ジャンルの追加・削除・表示名変更・優先順位の変更（タグが複数ジャンルに当たったときどちらが勝つか）・タグの移動ができる。変更するたびに既存記事が再分類され、何件動いたかが表示される。分類できなかった記事は「その他」ジャンルから辿れるので、実データを見ながら辞書を育てられる
- Saved ビューではタグごとのフィルタチップと「タグなし」チップを表示
- 記事リーダー末尾に「類似 Saved 記事」——タグ（手動タグが無ければ AI タグ候補）が 1 つ以上一致する Saved 記事からランダム 3 件を表示、クリックで右ペインに切り替え
- 本文取得失敗記事の確認・対処 UI——404 / 403 / 一時エラー / ユーザースキップで分類し、サイドバーのモーダルから記事ごとに **再試行 / 要約のみ / 削除** を選択可能（一括操作対応）。一時エラーは 5 分バックオフで自動再試行、恒久失敗は LLM 要約のブロックを解除し Phase 1 が RSS summary にフォールバックする
- OPML インポート / エクスポート
- Saved 記事インポート（Inoreader / Google Reader JSON 形式）
- フィード横断の重複記事整理——正規化 URL（トラッキングパラメータ・フラグメント除去）が一致する記事を重複とみなし、保管済み > 非はてなブックマーク由来 > 取得日時が古い方の優先順位で 1 件を残し、削除される側の既読・保管状態やタグはマージする。定期取得のたびに自動実行、サイドバーの「重複記事を整理」ボタンからドライラン確認付きで手動実行も可能
- キーボードショートカット（`j`/`k` ナビ、`s` 保管、`/` 検索）
- モバイル上部バーに現在のカテゴリ・ビュー総件数・全体の未読件数を表示
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
| Web 検索（オプション） | DuckDuckGo（`ddgs`） |

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
| `SNOREADER_LLM_REASONING_EFFORT` | `none` | 推論（thinking）の強度。`none` で thinking を無効化し、品質を落とさず要約を 4〜5 倍高速化する。パラメータを解釈しないサーバでは空文字を設定する |
| `SNOREADER_SUMMARIZE_INTERVAL_SECONDS` | `180` | バックグラウンド要約の実行間隔（秒） |
| `SNOREADER_SUMMARIZE_BATCH_SIZE` | `5` | 1 回の要約バッチ件数 |

LLM サーバーが利用可能な場合、SnoReader は以下を自動実行する：
- 記事の日本語箇条書き要約をバックグラウンドで生成（優先順：Saved > 未読 > 既読）
- AI 要約をもとにタグを提案
- 手動入力された日本語タグを英語に自動翻訳
- リーダーペイン下部のチャットパネルで記事に関する自由質問を受け付け（セッション内履歴のみ、記事切替でクリア）

### チャット Web 検索

チャットは記事本文を主な文脈としつつ、Web 検索結果と一般知識でも回答する。記事外の情報である場合はその旨を明示する。

`ddgs` 経由の DuckDuckGo 検索は、明示的な検索指示（`検索`、`調べて`、`search`）、時事性キーワード（`最新`、`latest`、「今…？」疑問文）、または説明要求（`とは`、`意味`、`背景`、`経緯`、`違い`、`なぜ`、`理由`、`what is`、`why`、`explain` など）が含まれる場合に実行される。記事本文の言い換えを求めるだけの入力（`要約`、`まとめて`、`結論`）は待ち時間を避けるため検索しない（明示的な検索指示があればそちらが優先）。上位 3 件を LLM コンテキストに注入し、回答と合わせてソースリンクを返す。検索失敗・タイムアウト時は記事本文と一般知識のみで回答する。

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
| `r` | 記事・フィードを再取得 |

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
│       │   ├── scheduler.py      #   APScheduler: フィード更新 + AI 要約
│       │   └── web_search.py     #   チャット用 DuckDuckGo 検索ヘルパー
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
│           └── articles/{ArticleList,ArticleCard,ArticleReader,ArticleChatPanel}.tsx
├── data/                         # SQLite DB（git 管理外）
├── certs/                        # TLS 証明書（git 管理外）
└── Makefile
```

## ライセンス

[MIT](LICENSE)
