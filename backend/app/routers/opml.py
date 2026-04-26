"""OPML import/export endpoints."""

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models import Feed
from app.services.feed_fetcher import fetch_feed

router = APIRouter(tags=["opml"])


@router.get("/opml/export")
async def export_opml(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Feed).order_by(Feed.title))
    feeds = result.scalars().all()

    opml = ET.Element("opml", version="2.0")
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = "SnoReader Subscriptions"
    ET.SubElement(head, "dateCreated").text = datetime.now(timezone.utc).isoformat()
    body = ET.SubElement(opml, "body")

    for feed in feeds:
        attrs = {"type": "rss", "xmlUrl": feed.url, "text": feed.title or feed.url}
        if feed.site_url:
            attrs["htmlUrl"] = feed.site_url
        ET.SubElement(body, "outline", **attrs)

    buf = BytesIO()
    tree = ET.ElementTree(opml)
    ET.indent(tree)
    tree.write(buf, encoding="UTF-8", xml_declaration=True)

    return Response(
        content=buf.getvalue(),
        media_type="application/xml",
        headers={"Content-Disposition": "attachment; filename=snoreader-feeds.opml"},
    )


@router.post("/opml/import")
async def import_opml(file: UploadFile, session: AsyncSession = Depends(get_session)):
    content = await file.read()
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        raise HTTPException(status_code=400, detail="Invalid XML")

    urls: list[str] = []
    for outline in root.iter("outline"):
        xml_url = outline.get("xmlUrl")
        if xml_url:
            urls.append(xml_url)

    if not urls:
        raise HTTPException(status_code=400, detail="No feeds found in OPML")

    created = 0
    skipped = 0
    for url in urls:
        existing = await session.execute(select(Feed).where(Feed.url == url))
        if existing.scalar_one_or_none():
            skipped += 1
            continue
        feed = Feed(url=url)
        session.add(feed)
        await session.commit()
        await session.refresh(feed)
        await fetch_feed(feed, session)
        created += 1

    return {"created": created, "skipped": skipped, "total": len(urls)}
