"""Extract clean article content from URLs using trafilatura."""

from __future__ import annotations

import logging
import re
from typing import Literal
from urllib.parse import urljoin

import httpx
import trafilatura

ExtractStatus = Literal["not_found", "forbidden", "error"]

logger = logging.getLogger(__name__)

_GRAPHIC_RE = re.compile(r'<graphic\b([^>]*)/?>', re.IGNORECASE)
_ATTR_SRC = re.compile(r'\bsrc="([^"]*)"')
_ATTR_ALT = re.compile(r'\balt="([^"]*)"')
_NESTED_PRE = re.compile(r'<pre>\s*<pre>(.*?)</pre>\s*</pre>', re.DOTALL)

_YAHOO_PICKUP_RE = re.compile(r'https?://news\.yahoo\.co\.jp/pickup/')
# Match canonical article URLs only — exclude sub-paths like /articles/HASH/images/000
_YAHOO_ARTICLE_RE = re.compile(r'^https?://news\.yahoo\.co\.jp/articles/[^/?#]+/?(?:[?#].*)?$')
# Matches URLs that should NOT be treated as the target article
_YAHOO_IGNORE_RE = re.compile(
    r'yimg\.jp|x\.com|twitter\.com|facebook\.com|instagram\.com|lycorp\.co\.jp|privacy',
    re.IGNORECASE,
)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

# <meta charset="..."> / <meta http-equiv="Content-Type" content="text/html; charset=..."> の宣言を拾う
_META_CHARSET_RE = re.compile(
    rb'<meta[^>]+?charset=["\']?\s*([A-Za-z0-9_\-:]+)',
    re.IGNORECASE,
)


def _fix_html(html: str, base_url: str = "") -> str:
    """Convert trafilatura-specific tags to standard HTML and fix image URLs."""
    def _graphic_to_img(m: re.Match) -> str:
        attrs = m.group(1)
        src = _ATTR_SRC.search(attrs)
        alt = _ATTR_ALT.search(attrs)
        src_val = src.group(1) if src else ""
        alt_val = alt.group(1) if alt else ""
        if base_url and src_val and not src_val.startswith(('http', '//', 'data:')):
            src_val = urljoin(base_url, src_val)
        return f'<img src="{src_val}" alt="{alt_val}" loading="lazy">'

    html = _GRAPHIC_RE.sub(_graphic_to_img, html)
    html = _NESTED_PRE.sub(r'<pre>\1</pre>', html)

    # Absolutize relative img src and add referrerpolicy for hotlink protection
    def _fix_img_tag(m: re.Match) -> str:
        tag = m.group(0)
        def _abs_src(sm: re.Match) -> str:
            src = sm.group(1)
            if base_url and src and not src.startswith(('http', '//', 'data:')):
                src = urljoin(base_url, src)
            return f'src="{src}"'
        tag = re.sub(r'src="([^"]*)"', _abs_src, tag)
        if 'referrerpolicy' not in tag:
            tag = tag.replace('<img', '<img referrerpolicy="no-referrer"', 1)
        return tag

    html = re.sub(r'<img\b[^>]*>', _fix_img_tag, html)
    return html


def _find_yahoo_next_url(html_bytes: bytes) -> str | None:
    """Find the next URL to follow from a Yahoo pickup or articles page.

    Priority:
    1. Yahoo article page (/articles/) — pickup pages link here
    2. External source URL — articles pages may link to the original publisher
    """
    try:
        from lxml import html as lxml_html
        tree = lxml_html.fromstring(html_bytes)
        hrefs = [a.get('href', '') for a in tree.xpath('//a[@href]')]
        for href in hrefs:
            if _YAHOO_ARTICLE_RE.match(href):
                return href
        for href in hrefs:
            if href.startswith('http') and not _YAHOO_IGNORE_RE.search(href):
                return href
    except Exception:
        pass
    return None


def _is_matome_blog(html_bytes: bytes) -> bool:
    """Livedoor Blog まとめブログパターン (スレッド書き込み列挙) を検出する。"""
    return (b'id="comments-list"' in html_bytes
            and b'class="comment-set"' in html_bytes)


def _extract_matome_posts(html_bytes: bytes, base_url: str) -> str | None:
    """Livedoor Blog まとめ記事の導入文 + スレッド書き込みを HTML で返す。"""
    try:
        from lxml import html as lxml_html
        from lxml.html import tostring as lxml_tostring

        tree = lxml_html.fromstring(html_bytes)
        tree.make_links_absolute(base_url)
        parts: list[str] = []

        # 導入文
        intro_nodes = tree.xpath('//div[contains(@class,"article-body-inner")]')
        if intro_nodes:
            intro_html = lxml_tostring(intro_nodes[0], encoding="unicode", method="html")
            intro_html = re.sub(r"^<div[^>]*>", '<div class="matome-intro">', intro_html, count=1)
            parts.append(intro_html)

        # スレッド書き込み
        post_items: list[str] = []
        for div in tree.xpath('//div[starts-with(@id,"com_")]'):
            div_id = div.get("id", "")
            post_num = div_id[4:] if div_id.startswith("com_") else ""
            for cs in div.xpath('.//li[contains(@class,"comment-body")]'):
                body_html = lxml_tostring(cs, encoding="unicode", method="html")
                body_inner = re.sub(r"^<li[^>]*>|</li>\s*$", "", body_html.strip())
                post_items.append(
                    f'<div class="thread-post">'
                    f'<span class="post-num">{post_num}</span>'
                    f'<div class="post-body">{body_inner}</div>'
                    f'</div>'
                )

        if post_items:
            parts.append('<div class="thread-posts">' + "".join(post_items) + "</div>")

        if not parts:
            return None
        return _fix_html("\n".join(parts), base_url=base_url)

    except Exception as e:
        logger.warning("まとめブログ抽出失敗 %s: %s", base_url, e)
        return None


def _extract_from_html(html: str | bytes, url: str) -> str | None:
    """Parse HTML (text or bytes) and extract main content as HTML string."""
    html_bytes = html if isinstance(html, bytes) else html.encode()

    # まとめブログパターン → カスタム抽出
    if _is_matome_blog(html_bytes):
        result = _extract_matome_posts(html_bytes, url)
        if result:
            return result

    tree = trafilatura.load_html(html)
    if tree is None:
        return None
    result = trafilatura.extract(
        tree,
        url=url,
        include_comments=False,
        include_tables=True,
        include_images=True,
        include_links=True,
        include_formatting=True,
        output_format="html",
    )
    # Retry with favor_recall for aggregator/bulletin-board sites where standard extraction fails
    if not result:
        result = trafilatura.extract(
            tree,
            url=url,
            include_comments=False,
            include_tables=True,
            include_images=True,
            include_links=True,
            include_formatting=True,
            favor_recall=True,
            output_format="html",
        )
    if result:
        result = _fix_html(result, base_url=url)
    return result


def _decoded_html(resp: httpx.Response) -> str | bytes:
    """Return HTML decoded with the response's declared encoding when available.

    trafilatura/lxml の自動検出は EUC-JP / Shift_JIS のページで誤判定して文字化け
    することがある。決定順は以下:

    1. HTTP レスポンスヘッダに charset がある → httpx の ``.text`` を使う
    2. HTML 内の ``<meta charset>`` 宣言を読み取り、その encoding で decode
    3. どちらも無ければ生バイトを返し、trafilatura 側の自動検出に任せる
    """
    if resp.charset_encoding:
        return resp.text
    m = _META_CHARSET_RE.search(resp.content[:4096])
    if m:
        declared = m.group(1).decode("ascii", errors="ignore").strip().lower()
        # Python は "shift_jis" も "sjis" も解決できるが、一部の別名はそうではない
        alias = {"shift-jis": "shift_jis", "x-sjis": "shift_jis"}
        encoding = alias.get(declared, declared)
        try:
            return resp.content.decode(encoding, errors="replace")
        except (LookupError, UnicodeDecodeError):
            pass
    return resp.content


async def extract_content(url: str) -> tuple[str | None, ExtractStatus | None]:
    """Fetch a URL and extract the main article text as HTML.

    Returns ``(html, None)`` on success, ``(None, status)`` on failure where
    status classifies the failure so callers can decide whether to retry:

    - ``"not_found"``  : HTTP 404 (permanent — the resource is gone)
    - ``"forbidden"``  : HTTP 403 (permanent — bot-detection / paywall)
    - ``"error"``      : 5xx / timeout / network error (transient — retry ok)

    Yahoo pickup pages link to /articles/<hash>, where the full body lives.
    We follow that single hop, then extract directly.
    """
    try:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            http2=False,
            headers=_BROWSER_HEADERS,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()

            # One hop: pickup → articles (or external source as legacy fallback)
            if _YAHOO_PICKUP_RE.search(str(resp.url)):
                next_url = _find_yahoo_next_url(resp.content)
                if next_url and next_url != str(resp.url):
                    logger.info("Yahoo: following %s → %s", resp.url, next_url)
                    try:
                        resp = await client.get(next_url)
                        resp.raise_for_status()
                    except Exception as e:
                        logger.warning("Failed to fetch %s: %s", next_url, e)

            return _extract_from_html(_decoded_html(resp), str(resp.url)), None

    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code
        logger.warning("HTTP %s fetching %s", status_code, url)
        if status_code == 404:
            return None, "not_found"
        if status_code == 403:
            return None, "forbidden"
        return None, "error"
    except Exception as e:
        logger.warning("Failed to fetch %s: %s", url, e)
        return None, "error"
