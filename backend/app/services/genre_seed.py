"""ジャンル定義の初期値。genres テーブルが空のときだけ投入する。

実データ（未読 617 件）で分布を確認済み。dev 19% / ai 16% / politics 10% /
incident 8% / other 8% / science 7% / life 7% / culture 7% / economy 6% /
entertainment 6% / sports 3% / security 3%。

投入後はユーザーが管理画面で編集する前提なので、シードは二度と走らせない。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Genre, GenreRule

# (key, label_ja, priority, tags)
GENRE_SEED: list[tuple[str, str, int, list[str]]] = [
    ("ai", "AI・LLM", 1,
     ["ai", "llm", "openai", "claude", "rag", "mcp", "genai", "chatgpt", "gemini", "nvidia"]),
    ("security", "セキュリティ", 2,
     ["security", "privacy", "vulnerability", "malware"]),
    ("dev", "開発・技術", 3,
     ["programming", "web", "javascript", "python", "rust", "unity", "database", "api",
      "github", "linux", "windows", "microsoft", "software", "hardware", "network",
      "excel", "performance", "cloud", "aws", "vscode", "it", "tools", "data"]),
    ("sports", "スポーツ", 4,
     ["baseball", "sports", "sport", "soccer"]),
    ("incident", "事件・災害", 5,
     ["disaster", "accident", "earthquake", "crime", "safety"]),
    ("politics", "政治・行政", 6,
     ["government", "politics", "policy", "geopolitics", "law", "war",
      "local-government", "copyright", "gender", "labor", "disability"]),
    ("economy", "経済・ビジネス", 7,
     ["finance", "economy", "business", "tax", "yen", "accounting", "payment",
      "marketing", "retail", "consumer", "career", "monetization"]),
    ("science", "科学・教育", 8,
     ["research", "psychology", "education", "university", "mathematics", "medical",
      "agriculture", "wildlife", "logic", "infection", "space", "animal"]),
    ("culture", "文化・歴史", 9,
     ["history", "museum", "architecture", "art", "literature", "design",
      "writing", "media", "culture"]),
    ("entertainment", "エンタメ", 10,
     ["entertainment", "game", "manga", "anime", "movie", "music", "comedy",
      "story", "science-fiction", "comic", "book"]),
    ("life", "生活・健康", 11,
     ["health", "life", "lifestyle", "daily-life", "food", "recipe", "travel",
      "relationship", "emotion", "mental-health", "home", "weather",
      "society", "social", "community", "communication", "social-media",
      "railway", "transportation"]),
]

# 手がかりの弱いタグ。通常ルールが 1 つも当たらなかったときだけ使う。
# news / japan / japanese はどのジャンルにも寄せず、ルール無しのまま other に落とす
GENERIC_SEED: dict[str, str] = {"technology": "dev"}


async def seed_genres(session: AsyncSession) -> int:
    """genres が空のときだけ初期辞書を投入し、作成したジャンル数を返す。"""
    existing = await session.scalar(select(func.count()).select_from(Genre))
    if existing:
        return 0

    by_key: dict[str, Genre] = {}
    for key, label_ja, priority, tags in GENRE_SEED:
        genre = Genre(key=key, label_ja=label_ja, priority=priority)
        session.add(genre)
        by_key[key] = genre
    await session.flush()

    for key, _label, _priority, tags in GENRE_SEED:
        for tag in tags:
            session.add(GenreRule(tag=tag, genre_id=by_key[key].id, is_generic=False))
    for tag, key in GENERIC_SEED.items():
        session.add(GenreRule(tag=tag, genre_id=by_key[key].id, is_generic=True))
    await session.flush()

    return len(by_key)
