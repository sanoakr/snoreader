"""URL exclude-pattern matching, shared by feed_fetcher.py and the exclude_patterns router.

Patterns are plain substrings with optional `*` globs (e.g. "tonarinoyj.jp/episode/*"),
matched case-insensitively against the full article URL.
"""

from __future__ import annotations

from fnmatch import fnmatch


def is_excluded(url: str, patterns: list[str]) -> bool:
    if not url or not patterns:
        return False
    low = url.lower()
    return any(fnmatch(low, f"*{p.lower()}*") for p in patterns)
