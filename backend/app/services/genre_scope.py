"""genre 指定をキー集合へ展開する。

親ジャンルの指定は「その親と全ての子孫」を意味する。一覧・一括既読・一括
dismiss の 3 箇所が同じ意味で動く必要があるため、展開はここ 1 箇所に置く。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Genre


async def genre_keys(
    session: AsyncSession, genre: str, *, exact: bool = False
) -> list[str]:
    """genre とその子孫のキー一覧を返す。

    - ``exact=True``: そのキーだけ（子を持つ親の直下だけを対象にしたいとき）
    - ``genres`` に行を持たないキー（予約キー ``"other"`` や削除済みジャンル）は
      そのまま 1 件で返す
    """
    if exact:
        return [genre]

    rows = (await session.execute(select(Genre.id, Genre.key, Genre.parent_id))).all()
    id_by_key = {key: gid for gid, key, _parent in rows}
    if genre not in id_by_key:
        return [genre]

    children_by_parent: dict[int, list[str]] = {}
    for _gid, key, parent_id in rows:
        if parent_id is not None:
            children_by_parent.setdefault(parent_id, []).append(key)

    keys = [genre]
    queue = [id_by_key[genre]]
    while queue:
        for child_key in children_by_parent.get(queue.pop(), []):
            if child_key in keys:
                continue  # 循環していても止まる
            keys.append(child_key)
            queue.append(id_by_key[child_key])
    return keys
