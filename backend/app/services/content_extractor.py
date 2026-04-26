"""Extract clean article content from URLs using trafilatura."""

from __future__ import annotations

import logging
import re

import httpx
import trafilatura

logger = logging.getLogger(__name__)

_GRAPHIC_RE = re.compile(r'<graphic\b([^>]*)/?>', re.IGNORECASE)
_ATTR_SRC = re.compile(r'\bsrc="([^"]*)"')
_ATTR_ALT = re.compile(r'\balt="([^"]*)"')
_NESTED_PRE = re.compile(r'<pre>\s*<pre>(.*?)</pre>\s*</pre>', re.DOTALL)

_YAHOO_PICKUP_RE = re.compile(r'https?://news\.yahoo\.co\.jp/pickup/')
_YAHOO_IGNORE_RE = re.compile(
    r'yahoo|yimg\.jp|x\.com|twitter\.com|facebook\.com|instagram\.com|lycorp\.co\.jp|privacy',
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


def _fix_html(html: str) -> str:
    """Convert trafilatura-specific tags to standard HTML."""
    def _graphic_to_img(m: re.Match) -> str:
        attrs = m.group(1)
        src = _ATTR_SRC.search(attrs)
        alt = _ATTR_ALT.search(attrs)
        src_val = src.group(1) if src else ""
        alt_val = alt.group(1) if alt else ""
        return f'<img src="{src_val}" alt="{alt_val}" loading="lazy" style="max-width:100%">'

    html = _GRAPHIC_RE.sub(_graphic_to_img, html)
    html = _NESTED_PRE.sub(r'<pre>\1</pre>', html)
    return html


def _find_yahoo_pickup_article_url(html_bytes: bytes) -> str | None:
    """Extract the external article link from a Yahoo pickup page."""
    try:
        from lxml import html as lxml_html
        tree = lxml_html.fromstring(html_bytes)
        for a in tree.xpath('//a[@href]'):
            href = a.get('href', '')
            if href.startswith('http') and not _YAHOO_IGNORE_RE.search(href):
                return href
    except Exception:
        pass
    return None


def _extract_from_bytes(content: bytes, url: str) -> str | None:
    """Parse bytes with proper encoding detection and extract content."""
    tree = trafilatura.load_html(content)
    if tree is None:
        return None
    result = trafilatura.extract(
        tree,
        url=url,
        include_comments=False,
        include_tables=True,
        include_images=True,
        include_links=True,
        output_format="html",
    )
    if result:
        result = _fix_html(result)
    return result


async def extract_content(url: str) -> str | None:
    """Fetch a URL and extract the main article text as HTML."""
    try:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            http2=False,
            headers=_BROWSER_HEADERS,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()

            # Yahoo pickup pages link to the actual article on an external site
            if _YAHOO_PICKUP_RE.search(str(resp.url)):
                article_url = _find_yahoo_pickup_article_url(resp.content)
                if article_url:
                    logger.info("Yahoo pickup: following external link %s", article_url)
                    try:
                        resp = await client.get(article_url)
                        resp.raise_for_status()
                    except Exception as e:
                        logger.warning("Failed to fetch Yahoo article source %s: %s", article_url, e)

            return _extract_from_bytes(resp.content, str(resp.url))

    except Exception as e:
        logger.warning("Failed to fetch %s: %s", url, e)
        return None
