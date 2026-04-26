"""Extract clean article content from URLs using trafilatura."""

from __future__ import annotations

import logging

import httpx
import trafilatura

logger = logging.getLogger(__name__)


async def extract_content(url: str) -> str | None:
    """Fetch a URL and extract the main article text."""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception as e:
        logger.warning("Failed to fetch %s: %s", url, e)
        return None

    text = trafilatura.extract(
        resp.text,
        include_comments=False,
        include_tables=True,
        include_images=True,
        include_links=True,
        output_format="html",
    )
    return text
