"""FastAPI application entry point."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.database import engine
from app.models import Base
from app.routers import articles, feeds, imports, opml, tags
from app.services.scheduler import start_scheduler, stop_scheduler

FTS_SETUP = """
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title, summary, content,
    content='articles',
    content_rowid='id'
);
"""

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
        await conn.execute(text(FTS_SETUP))
        for trigger_sql in FTS_TRIGGERS:
            await conn.execute(text(trigger_sql))

    start_scheduler()
    yield
    stop_scheduler()


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
