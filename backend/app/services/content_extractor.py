"""Extract clean article content from URLs using trafilatura."""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

import httpx
import trafilatura

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


def _extract_from_html(html: str | bytes, url: str) -> str | None:
    """Parse HTML (text or bytes) and extract main content as HTML string."""
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
    することがある。HTTP レスポンスの Content-Type に charset がある場合は httpx
    がそれに従って ``.text`` を返すため、テキストを優先して trafilatura に渡す。
    宣言が無く検出にも失敗した場合のみ生バイトへフォールバックする。
    """
    if resp.charset_encoding:
        return resp.text
    return resp.content


async def extract_content(url: str) -> str | None:
    """Fetch a URL and extract the main article text as HTML.

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

            return _extract_from_html(_decoded_html(resp), str(resp.url))

    except Exception as e:
        logger.warning("Failed to fetch %s: %s", url, e)
        return None
