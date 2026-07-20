# 未保存既読記事の自動削除 — 設計

## 背景・目的

SnoReader はフィードを継続的に取得し続けるため、記事テーブルが際限なく増え続ける。ユーザーが明示的に「保存」した記事は資料的価値があるため残すべきだが、既読かつ未保存の記事は読み終わった一時的な情報であり、一定期間が過ぎたら自動的に削除してストレージ肥大化を防ぐ。

## 削除条件

以下をすべて満たす `Article` 行を削除対象とする。

- `is_saved == False`
- `is_read == True`
- 基準日時が「現在時刻 − 保持日数」より前
  - 基準日時は `published_at` を優先する
  - `published_at` が `NULL`（フィードが日付を提供していない）の場合は `fetched_at` を代わりに使う

保持日数はデフォルト90日。`未読`の記事は経過日数に関わらず対象外。`保存済み`の記事は既読・未読を問わず対象外。

## 設定

`app/config.py` の `Settings` に以下を追加する。

```python
article_retention_days: int = 90
```

環境変数 `SNOREADER_ARTICLE_RETENTION_DAYS` で上書き可能（既存の `SNOREADER_` プレフィックス規約に従う）。

## 実装

新規モジュール `backend/app/services/article_cleanup.py` を追加し、単一の関数を定義する。

```python
async def cleanup_old_articles(session: AsyncSession) -> int:
    """未保存かつ既読で、保持期間を過ぎた記事を削除し、削除件数を返す。"""
```

- 基準日時の比較は文字列（ISO8601）のまま行える（`published_at`/`fetched_at` は既存モデルで文字列カラムのため、`Article.published_at`, `Article.fetched_at` はいずれも `datetime.now(timezone.utc)` と同じ `isoformat()` 形式で保存されている）。カットオフ時刻を `(datetime.now(timezone.utc) - timedelta(days=settings.article_retention_days)).isoformat()` として計算し、`COALESCE(published_at, fetched_at) < cutoff` に相当する条件で絞り込む。
- 削除は SQLAlchemy の `delete(Article).where(...)` によるバルク削除で行う。`article_tags.article_id` は `ForeignKey("articles.id", ondelete="CASCADE")`（DBレベル制約、`models.py:90`）のため、バルク削除でも `ArticleTag` 行は自動的に削除される。
- FTS5 (`articles_fts`) の同期トリガーは既存の DELETE トリガーでそのまま追従する想定。
- 削除件数を `logger.info("Cleaned up %d old unsaved articles (retention=%d days)", count, settings.article_retention_days)` の形でログ出力する。

## 呼び出し箇所

`backend/app/services/feed_fetcher.py` の `fetch_all_feeds()` 末尾、`dedup_articles(session)` 呼び出しの直後に `cleanup_old_articles(session)` を呼ぶ1行を追加する。既存の「fetch → dedup」の流れに「fetch → dedup → cleanup」として乗せる。

```python
async with async_session() as session:
    await dedup_articles(session)
    await cleanup_old_articles(session)
```

## テスト

新規 `backend/tests/test_article_cleanup.py` に以下のケースを追加する。

1. `published_at` が91日前、`is_read=True`, `is_saved=False` → 削除される
2. 同条件だが `is_saved=True` → 削除されない
3. 同条件だが `is_read=False` → 削除されない
4. `published_at` が89日前、`is_read=True`, `is_saved=False` → 削除されない（境界内）
5. `published_at IS NULL`、`fetched_at` が91日前、`is_read=True`, `is_saved=False` → 削除される
6. `article_retention_days` を環境変数で上書きした場合に反映されること

## スコープ外

- 削除前のユーザー通知・確認UIは作らない（バックグラウンド処理としてサイレントに実行）。
- 削除の取り消し（ゴミ箱的な仕組み）は作らない。
- フロントエンドの変更は不要（既存のフィルタ・一覧は削除後の状態をそのまま表示するだけ）。
