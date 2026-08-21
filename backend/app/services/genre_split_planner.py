"""未読が上限を超えた葉ジャンルの分割案を作る。

DB には触らない純関数。分類は既存 genre_classifier.classify を使い、
候補ルールに対して未読記事全件を実際に分類し直してから件数を出す
（推測値は出さない）。兄弟ジャンルは親と同じ priority を持つため
同順位になり、_resolve の同値解決（キーの辞書順）で決まる——
受け皿より後にソートされるキーの新兄弟は記事を 1 件も取れない。
シミュレーションを通せばこの失敗は projected 件数 0 として自動検出される。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace

from app.services.genre_classifier import OTHER_GENRE, GenreRules, classify

# 下限なしだと 2 件のジャンルが量産される（実データで未ルール共起タグは waymo 2 / google 2 級）
_MIN_CHILD_ARTICLES = 8
# 1 提案で辞書が大きく動きすぎないようにする
_MAX_NEW_CHILDREN = 4
# 貪欲詰めの 1 ビンあたり上限（limit のこの割合まで）。分割直後に再超過しないための余裕
_BIN_FILL_RATIO = 0.8
# 新しい兄弟ジャンルに割り当てる priority のデフォルト値（親に priority が無い場合の保険）
_DEFAULT_NEW_GENRE_PRIORITY = 100
# そのジャンルの未読の何割以上に出現するタグを「受け皿」とみなすか
_DEMOTE_COVERAGE = 0.8


@dataclass(frozen=True)
class ProposedChild:
    key: str
    label_ja: str
    tags: tuple[str, ...]
    # 推定値ではない: candidate ルールで classify を実際に再実行して測定した件数
    estimated_unread: int


@dataclass(frozen=True)
class SplitProposal:
    genre_key: str
    strategy: str
    before: int
    projected_max: int
    children: tuple[ProposedChild, ...]
    demote_tags: tuple[str, ...] = ()


def _leaf_keys(rules: GenreRules) -> set[str]:
    """子を持たないジャンルキー。parent の値に現れるキーは親なので除く。"""
    all_keys = set(rules.priority)
    parents = set(rules.parent.values())
    return all_keys - parents


def _current_counts(articles: list[tuple[int, list[str]]], rules: GenreRules) -> Counter[str]:
    return Counter(classify(tags, rules) for _aid, tags in articles)


def _simulate(
    articles: list[tuple[int, list[str]]],
    rules: GenreRules,
    *,
    tag_moves: dict[str, str],
    demote: set[str],
    new_priorities: dict[str, int] | None = None,
    new_parents: dict[str, str] | None = None,
) -> Counter[str]:
    """候補ルールで実際に分類し直した件数を返す。推測しない。

    new_parents は新しく提案する子キー -> 親キーの対応。これを渡さないと
    新キーが rules.parent に登録されないまま classify に渡り、_prune_ancestors
    の「祖先と子孫が混在したら祖先を落とす」規則が働かず、適用後の実際の
    分類結果とシミュレーションが食い違ってしまう。
    """
    tag_to_genre = dict(rules.tag_to_genre)
    generic_to_genre = dict(rules.generic_to_genre)
    for tag, genre in tag_moves.items():
        tag_to_genre[tag] = genre
    for tag in demote:
        if tag in tag_to_genre:
            generic_to_genre[tag] = tag_to_genre.pop(tag)
    priority = dict(rules.priority)
    priority.update(new_priorities or {})
    parent = dict(rules.parent)
    parent.update(new_parents or {})
    candidate = replace(
        rules,
        tag_to_genre=tag_to_genre,
        generic_to_genre=generic_to_genre,
        priority=priority,
        parent=parent,
    )
    return Counter(classify(tags, candidate) for _aid, tags in articles)


def _affected_max(current: Counter[str], projected: Counter[str], genre_key: str) -> int:
    """この案が影響するバケットの最大件数。

    corpus 全体の最大値ではない。無関係なジャンルが上限を超えているだけで
    正しい案が棄却されるのを防ぐ。「譲られた側が溢れないこと」を見るために、
    件数が変化したジャンル（受け取った側・失った側の両方）と対象ジャンル自身だけを見る。
    """
    changed = {k for k in set(current) | set(projected) if current[k] != projected[k]}
    changed.add(genre_key)
    return max((projected[k] for k in changed), default=0)


def _own_tags(genre_key: str, rules: GenreRules) -> list[str]:
    return [t for t, g in rules.tag_to_genre.items() if g == genre_key]


def _sibling_key(parent_key: str, tag: str) -> str:
    """新しい兄弟のキー。親キーの接頭辞を保ち、タグ名の非英数字を _ にする。"""
    slug = "".join(ch if ch.isalnum() else "_" for ch in tag.lower())
    return f"{parent_key}_{slug}"


def _plan_split_own_tags(
    genre_key: str,
    articles: list[tuple[int, list[str]]],
    rules: GenreRules,
    *,
    limit: int,
) -> SplitProposal | None:
    """担当タグを件数降順に貪欲に詰め、最多タグは元ジャンルに残す。"""
    current = _current_counts(articles, rules)
    before = current[genre_key]
    own = _own_tags(genre_key, rules)
    if len(own) < 2:
        return None

    # このジャンルに落ちている記事だけを見てタグ件数を数える
    mine = [(aid, tags) for aid, tags in articles if classify(tags, rules) == genre_key]
    tag_counts = Counter(t for _aid, tags in mine for t in tags if t in own)
    ranked = [t for t, _c in tag_counts.most_common() if tag_counts[t] > 0]
    if len(ranked) < 2:
        return None

    # 最多タグ（受け皿）は元ジャンルに残す。残りを貪欲にビンへ詰める
    movable = ranked[1:]
    bin_cap = max(1, int(limit * _BIN_FILL_RATIO))
    parent_key = rules.parent.get(genre_key, genre_key)

    bins: list[list[str]] = []
    for tag in movable:
        for b in bins:
            if sum(tag_counts[t] for t in b) + tag_counts[tag] <= bin_cap:
                b.append(tag)
                break
        else:
            if len(bins) >= _MAX_NEW_CHILDREN:
                # 子の上限に達しても、この後の（より小さい）タグは既存ビンの
                # 残り容量に収まる可能性がある。ここで打ち切ると取りこぼす
                continue
            bins.append([tag])
    if not bins:
        return None

    tag_moves: dict[str, str] = {}
    keys: list[str] = []
    seen_keys: set[str] = set()
    for b in bins:
        key = _sibling_key(parent_key, b[0])
        # 受け皿と衝突、既存の無関係なジャンルと衝突、この提案内での重複は
        # いずれも作れない（既存ジャンルとの衝突は誤って統合・上書きするデータ破損になる）
        if key == genre_key or key in rules.priority or key in seen_keys:
            return None
        seen_keys.add(key)
        keys.append(key)
        for tag in b:
            tag_moves[tag] = key

    projected = _simulate(
        articles,
        rules,
        tag_moves=tag_moves,
        demote=set(),
        new_priorities={
            k: rules.priority.get(parent_key, _DEFAULT_NEW_GENRE_PRIORITY) for k in keys
        },
        new_parents={k: parent_key for k in keys},
    )
    children = tuple(
        ProposedChild(key=key, label_ja=b[0], tags=tuple(b), estimated_unread=projected[key])
        for key, b in zip(keys, bins)
    )
    # 1 件も引き取れない子が混ざる案は、キーの辞書順で負けている。棄却する
    if any(c.estimated_unread == 0 for c in children):
        return None
    if projected[genre_key] > limit:
        return None
    # movable[0] (= ranked[0] の次) 以降を各ビンに詰めているので、最多タグは
    # ranked[0] のまま元ジャンルに残っている（tag_moves に含めていない）
    return SplitProposal(
        genre_key=genre_key,
        strategy="split_own_tags",
        before=before,
        projected_max=_affected_max(current, projected, genre_key),
        children=children,
    )


def _plan_demote_generic(
    genre_key: str,
    articles: list[tuple[int, list[str]]],
    rules: GenreRules,
    *,
    limit: int,
) -> SplitProposal | None:
    """受け皿タグを is_generic に降格し、通常ルールを持つ他ジャンルに譲る。

    ジャンルを 1 つも増やさずに済むので最優先で試す。ただし
    「AI＋セキュリティの記事は security へ行く」という意味の変更を伴うため、
    採用するかどうかはユーザーが決める（案として並べるだけ）。
    """
    current = _current_counts(articles, rules)
    before = current[genre_key]
    mine = [(aid, tags) for aid, tags in articles if classify(tags, rules) == genre_key]
    if not mine:
        return None
    own = set(_own_tags(genre_key, rules))
    if not own:
        return None

    threshold = len(mine) * _DEMOTE_COVERAGE
    counts = Counter(t for _aid, tags in mine for t in tags if t in own)
    receptacle = tuple(sorted(t for t, c in counts.items() if c >= threshold))
    if not receptacle:
        return None

    projected = _simulate(articles, rules, tag_moves={}, demote=set(receptacle))
    projected_max = _affected_max(current, projected, genre_key)
    if projected[genre_key] > limit or projected_max > limit:
        return None
    return SplitProposal(
        genre_key=genre_key,
        strategy="demote_generic",
        before=before,
        projected_max=projected_max,
        children=(),
        demote_tags=receptacle,
    )


def _plan_promote_free_tags(
    genre_key: str,
    articles: list[tuple[int, list[str]]],
    rules: GenreRules,
    *,
    limit: int,
) -> SplitProposal | None:
    """そのジャンルの記事に多く共起する未ルールタグを、新しいジャンルの担当にする。

    other は genres に行を持たない予約キーなので、新しい兄弟ではなく
    新しいトップレベルジャンルを提案する（階層は 2 段のまま）。
    """
    current = _current_counts(articles, rules)
    before = current[genre_key]
    mine = [(aid, tags) for aid, tags in articles if classify(tags, rules) == genre_key]
    if not mine:
        return None

    ruled = set(rules.tag_to_genre) | set(rules.generic_to_genre)
    counts = Counter(t for _aid, tags in mine for t in tags if t not in ruled)
    candidates = [(t, c) for t, c in counts.most_common() if c >= _MIN_CHILD_ARTICLES]
    candidates = candidates[:_MAX_NEW_CHILDREN]
    if not candidates:
        return None

    is_other = genre_key == OTHER_GENRE
    parent_key = genre_key if is_other else rules.parent.get(genre_key, genre_key)
    tag_moves: dict[str, str] = {}
    keys: list[str] = []
    seen_keys: set[str] = set()
    for tag, _c in candidates:
        key = tag if is_other else _sibling_key(parent_key, tag)
        # 既存キーと衝突、この提案内での重複はいずれも作れない
        if key == genre_key or key in rules.priority or key in seen_keys:
            return None
        seen_keys.add(key)
        keys.append(key)
        tag_moves[tag] = key

    # other 由来の新トップレベルは既定の priority とし、既存ジャンルを侵さない。
    # 兄弟のときは親と同じ priority（既存のサブジャンルと同じ作法）
    new_priority = (
        _DEFAULT_NEW_GENRE_PRIORITY
        if is_other
        else rules.priority.get(parent_key, _DEFAULT_NEW_GENRE_PRIORITY)
    )
    projected = _simulate(
        articles,
        rules,
        tag_moves=tag_moves,
        demote=set(),
        new_priorities={k: new_priority for k in keys},
    )
    children = tuple(
        ProposedChild(key=key, label_ja=tag, tags=(tag,), estimated_unread=projected[key])
        for key, (tag, _c) in zip(keys, candidates)
    )
    if any(c.estimated_unread == 0 for c in children):
        return None  # キーの辞書順で負けている案
    if projected[genre_key] > limit:
        return None
    projected_max = _affected_max(current, projected, genre_key)
    if projected_max > limit:
        return None
    return SplitProposal(
        genre_key=genre_key,
        strategy="promote_free_tags",
        before=before,
        projected_max=projected_max,
        children=children,
    )


def plan_splits(
    articles: list[tuple[int, list[str]]],
    rules: GenreRules,
    *,
    limit: int,
) -> list[SplitProposal]:
    """上限を超えた葉ジャンルごとに、成立した分割案を全部返す。

    articles は (article_id, tags) の列。rules のスナップショットで分類した
    結果が上限を超えたジャンルだけを対象にする。案は projected_max 昇順。
    """
    counts = _current_counts(articles, rules)
    leaves = _leaf_keys(rules) | {OTHER_GENRE}
    over = sorted(k for k, c in counts.items() if c > limit and k in leaves)

    proposals: list[SplitProposal] = []
    for genre_key in over:
        for planner in (_plan_demote_generic, _plan_split_own_tags, _plan_promote_free_tags):
            found = planner(genre_key, articles, rules, limit=limit)
            if found:
                proposals.append(found)
    proposals.sort(key=lambda p: (p.projected_max, p.genre_key, p.strategy))
    return proposals
