"""FastAPI application entry point."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.database import engine
from app.models import Base
from app.ai import task_queue
from app.routers import articles, feeds, imports, opml, tags
from app.services.background_processor import start as start_bg_processor
from app.services.background_processor import stop as stop_bg_processor
from app.services.scheduler import start_scheduler, stop_scheduler

# trigram トークナイザは CJK の部分一致検索に対応する（unicode61 では空白で
# 区切られない日本語が 1 トークン化されてしまい部分一致できない）
FTS_TOKENIZER = "trigram"

FTS_SETUP = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title, summary, content,
    content='articles',
    content_rowid='id',
    tokenize='{FTS_TOKENIZER}'
);
"""

FTS_TRIGGER_NAMES = ("articles_fts_ai", "articles_fts_au", "articles_fts_ad")

FTS_TRIGGERS = [
    """CREATE TRIGGER IF NOT EXISTS articles_fts_ai AFTER INSERT ON articles BEGIN
        INSERT INTO articles_fts(rowid, title, summary, content)
        VALUES (new.id, new.title, new.summary, new.content);
    END;""",
    """CREATE TRIGGER IF NOT EXISTS articles_fts_au AFTER UPDATE ON articles BEGIN
        INSERT INTO articles_fts(articles_fts, rowid, title, summary, content)
        VALUES ('delete', old.id, old.title, old.summary, old.content);
        INSERT INTO articles_fts(rowid, title, summary, content)
        VALUES (new.id, new.title, new.summary, new.content);
    END;""",
    """CREATE TRIGGER IF NOT EXISTS articles_fts_ad AFTER DELETE ON articles BEGIN
        INSERT INTO articles_fts(articles_fts, rowid, title, summary, content)
        VALUES ('delete', old.id, old.title, old.summary, old.content);
    END;""",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # 既存 DB に追加カラムがなければ ALTER TABLE で足す（create_all は追加しないため）
        col_rows = await conn.execute(text("PRAGMA table_info(articles)"))
        existing_article_cols = {row[1] for row in col_rows.fetchall()}
        if "extract_status" not in existing_article_cols:
            await conn.execute(text("ALTER TABLE articles ADD COLUMN extract_status TEXT"))
        if "extract_attempts" not in existing_article_cols:
            await conn.execute(
                text("ALTER TABLE articles ADD COLUMN extract_attempts INTEGER DEFAULT 0")
            )

        # 既存 FTS テーブルが古いトークナイザのまま残っていれば作り直す
        existing = await conn.execute(
            text("SELECT sql FROM sqlite_master WHERE name='articles_fts'")
        )
        existing_sql = (existing.scalar() or "").lower()
        needs_rebuild = False
        if existing_sql and FTS_TOKENIZER not in existing_sql:
            for trigger_name in FTS_TRIGGER_NAMES:
                await conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name}"))
            await conn.execute(text("DROP TABLE IF EXISTS articles_fts"))
            needs_rebuild = True
        elif not existing_sql:
            needs_rebuild = True

        await conn.execute(text(FTS_SETUP))
        for trigger_sql in FTS_TRIGGERS:
            await conn.execute(text(trigger_sql))

        if needs_rebuild:
            # 既存 articles を新しいトークナイザで再インデックス化
            await conn.execute(
                text("INSERT INTO articles_fts(articles_fts) VALUES('rebuild')")
            )

    task_queue.start()
    start_scheduler()
    start_bg_processor()
    yield
    stop_bg_processor()
    stop_scheduler()
    task_queue.stop()


app = FastAPI(title="SnoReader", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(feeds.router, prefix="/api")
app.include_router(articles.router, prefix="/api")
app.include_router(tags.router, prefix="/api")
app.include_router(opml.router, prefix="/api")
app.include_router(imports.router, prefix="/api")

# Serve frontend build in production (when frontend/dist exists)
_frontend_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _frontend_dist.is_dir():
    from starlette.responses import FileResponse
    from starlette.staticfiles import StaticFiles

    _assets_dir = _frontend_dist / "assets"
    if _assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=_assets_dir), name="static-assets")

    @app.get("/{path:path}")
    async def spa(path: str):
        file_path = (_frontend_dist / path).resolve()
        if file_path.is_file() and str(file_path).startswith(str(_frontend_dist.resolve())):
            return FileResponse(file_path)
        return FileResponse(_frontend_dist / "index.html")
