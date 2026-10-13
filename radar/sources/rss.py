"""RSS 2.0 and Atom. Untrusted XML: documents declaring a DOCTYPE or ENTITY are refused outright
(entity expansion is the one real attack on a parser that only reads feeds)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import timedelta
from urllib.parse import urlparse

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


def _raw(node: ET.Element, *names: str) -> str:
    for child in node:
        if _local(child.tag) in names and (child.text or "").strip():
            return child.text
    return ""


def _bullets(html: str) -> list[str]:
    """Release notes as one line per bullet: split the HTML on list items, paragraphs and breaks."""
    bullets = []
    for part in re.split(r"</li>|</p>|<br\s*/?>|\n", html):
        text = " ".join(TAG.sub(" ", part).replace("&nbsp;", " ").strip(" -*\t").split())
        if text:
            bullets.append(text)
    return bullets


ENCODING = re.compile(rb"""encoding\s*=\s*["']([^"']+)""")


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
    declared = ENCODING.search(raw[:200])
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in raw[:100] or (declared and declared.group(1).lower() not in (b"utf-8", b"us-ascii")):
        raise SourceError(f"{source.id}: only UTF-8 feeds are read (a wide encoding would hide a DOCTYPE from the check below)")
    if b"<!doctype" in head or b"<!entity" in head or b"<!entity" in raw[:200_000].lower():
        raise SourceError(f"{source.id}: feed declares a DOCTYPE/ENTITY; refused")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SourceError(f"{source.id}: not valid XML ({exc})") from exc
    entries = [n for n in root.iter() if _local(n.tag) in ("item", "entry")]
    allowed = set(source.opt("allowed_hosts", []))
    items = []
    for node in entries:
        title, url = _text(node, "title"), _link(node)
        if not title or not url.startswith("https://"):
            continue
        if allowed and urlparse(url).hostname not in allowed:
            continue                  # a mirror may only point at the vendor's own site
        guid = _text(node, "guid", "id") or url
        extra = {}
        if source.opt("release_notes"):
            extra = {"version": title, "bullets": _bullets(_raw(node, "content", "summary", "description"))}
        items.append(Item(id=f"{source.id}:{guid}", source=source.id, title=title, url=url,
                          published=parse_time(_text(node, "pubDate", "published", "updated", "date")),
                          body=_text(node, "description", "summary", "content")[:2000], extra=extra))
    return items


def collect(source: Source, ctx: Context):
    items = parse(ctx.fetch(source.url, max_bytes=source.opt("max_bytes", 5_000_000)), source)
    cutoff = ctx.now - timedelta(days=source.opt("max_age_days", 14))
    return [i for i in items if i.published is None or i.published >= cutoff], []
