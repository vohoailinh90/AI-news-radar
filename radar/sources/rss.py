"""RSS 2.0 and Atom. Untrusted XML: documents declaring a DOCTYPE or ENTITY are refused outright
(entity expansion is the one real attack on a parser that only reads feeds)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import timedelta

from ..config import Source
from ..models import Item
from . import Context, SourceError, parse_time

TAG = re.compile(r"<[^>]+>")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(node: ET.Element, *names: str) -> str:
    for child in node:
        if _local(child.tag) in names and (child.text or "").strip():
            return TAG.sub(" ", child.text).strip()
    return ""


def _link(node: ET.Element) -> str:
    for child in node:
        if _local(child.tag) == "link":
            if child.get("href") and child.get("rel", "alternate") == "alternate":
                return child.get("href", "").strip()
            if (child.text or "").strip():
                return child.text.strip()
    return ""


def parse(raw: bytes, source: Source) -> list[Item]:
    head = raw[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in head or b"<!entity" in raw[:200_000].lower():
        raise SourceError(f"{source.id}: feed declares a DOCTYPE/ENTITY; refused")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SourceError(f"{source.id}: not valid XML ({exc})") from exc
    entries = [n for n in root.iter() if _local(n.tag) in ("item", "entry")]
    items = []
    for node in entries:
        title, url = _text(node, "title"), _link(node)
        if not title or not url.startswith("https://"):
            continue
        guid = _text(node, "guid", "id") or url
        items.append(Item(id=f"{source.id}:{guid}", source=source.id, title=title, url=url,
                          published=parse_time(_text(node, "pubDate", "published", "updated", "date")),
                          body=_text(node, "description", "summary", "content")[:2000]))
    return items


def collect(source: Source, ctx: Context):
    items = parse(ctx.fetch(source.url, max_bytes=source.opt("max_bytes", 5_000_000)), source)
    cutoff = ctx.now - timedelta(days=source.opt("max_age_days", 14))
    return [i for i in items if i.published is None or i.published >= cutoff], []
