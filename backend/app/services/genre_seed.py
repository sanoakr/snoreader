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


# 推奨サブジャンル: (親 key, [(子 key, 子 label_ja, タグ)])
# 実データのタグ分布（2026-08-17、未読 42/34 件）から作った。親の代表タグ
# （ai / technology など）も子へ降ろして親を純粋な入れ物にする——降ろさないと
# 最大の束が分割前とほぼ変わらない。
#
# 兄弟は親と同じ priority を持つので必ず同順位になり、_resolve の同値解決
# （キーの辞書順）で決まる。したがって「受け皿」の子は、具体的な兄弟より
# 後にソートされるキーを付けないと具体的な兄弟の記事を吸ってしまう。
# ai の受け皿は ai_misc（ai_infra < ai_llm < ai_misc）。dev の受け皿
# dev_general は technology が汎用ルールで、通常ルールの兄弟と同じ段で
# 競合しないため改名の必要がない。
SUBGENRE_SEED: list[tuple[str, list[tuple[str, str, list[str]]]]] = [
    ("ai", [
        ("ai_llm", "LLM・生成AI",
         ["llm", "openai", "claude", "chatgpt", "gemini", "genai", "rag", "mcp"]),
        ("ai_misc", "AI 全般", ["ai"]),
        ("ai_infra", "AI ハードウェア", ["nvidia"]),
    ]),
    ("dev", [
        ("dev_prog", "プログラミング",
         ["programming", "python", "rust", "javascript", "web", "api", "github",
          "vscode", "unity"]),
        ("dev_infra", "クラウド・インフラ",
         ["cloud", "aws", "linux", "windows", "microsoft", "network"]),
        ("dev_data", "データ・DB", ["database", "data", "excel"]),
        ("dev_tools", "ツール・ハード",
         ["tools", "software", "it", "performance", "hardware"]),
        ("dev_general", "技術一般", ["technology"]),
    ]),
]


async def seed_subgenres(session: AsyncSession) -> tuple[int, int]:
    """推奨サブジャンルを冪等に投入し、(作成した子数, 付け替えたルール数) を返す。

    - 既に存在する子キーには触らない
    - タグの付け替えは「現在その親ジャンルに属しているタグ」だけを対象にする。
      別ジャンルにあるものは利用者が移したか元から別扱いなので動かさない。
      この規則なら「利用者が動かしたのか、まだ投入していないのか」を区別する
      必要がない
    - is_generic は元のルールの値を保つ（technology は汎用のまま子へ移る）
    - commit と再分類は呼び出し側が行う
    """
    created = 0
    moved = 0
    for parent_key, children in SUBGENRE_SEED:
        parent = (
            await session.execute(select(Genre).where(Genre.key == parent_key))
        ).scalar_one_or_none()
        if parent is None or parent.parent_id is not None:
            continue  # 未定義の親、または既に子になっている親は対象外

        for child_key, child_label, tags in children:
            child = (
                await session.execute(select(Genre).where(Genre.key == child_key))
            ).scalar_one_or_none()
            if child is None:
                child = Genre(
                    key=child_key,
                    label_ja=child_label,
                    priority=parent.priority,
                    parent_id=parent.id,
                )
                session.add(child)
                await session.flush()
                created += 1

            for tag in tags:
                rule = (
                    await session.execute(select(GenreRule).where(GenreRule.tag == tag))
                ).scalar_one_or_none()
                if rule is None or rule.genre_id != parent.id:
                    continue
                rule.genre_id = child.id
                moved += 1
    await session.flush()
    return created, moved
