"""RSS 2.0 and Atom. Untrusted XML: documents declaring a DOCTYPE or ENTITY are refused outright
(entity expansion is the one real attack on a parser that only reads feeds)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from xml.parsers import expat
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


NOISE = re.compile(r"^(?:full changelog|what.s changed|new contributors)\b|made their first contribution", re.I)
TRAILER = re.compile(r"(?:\s+by\s+@[\w-]+(?:\s+in\b.*)?|\s*\(#\d+\))\s*$", re.I)     # "... by @alice in <PR link>", "(#123)"


def _bullets(html: str) -> list[str]:
    """Release notes as one line per bullet: split the HTML at every block tag (so headings never swallow a bullet
    and nested lists become their own bullets)."""
    bullets = []
    for part in re.split(r"</?(?:li|ul|ol|p|h[1-6]|br|div|tr)\b[^>]*>|\n", html):
        text = " ".join(TAG.sub(" ", part).replace("&nbsp;", " ").strip(" -*\t").split())
        text = TRAILER.sub("", text).strip()
        if text and not NOISE.search(text):
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


def _refuse_dtd(raw: bytes, source: Source) -> None:
    """Entity expansion is the one real attack on a parser that only reads feeds. Let expat itself say whether the
    document declares a DOCTYPE or an ENTITY, anywhere in the body (padding, comments, CDATA and the encoding
    cannot hide one from the parser that would expand it). Malformed XML is left for ElementTree to report."""
    def reject(*_):
        raise SourceError(f"{source.id}: feed declares a DOCTYPE/ENTITY; refused")
    guard = expat.ParserCreate()
    guard.StartDoctypeDeclHandler = reject
    guard.EntityDeclHandler = reject
    try:
        guard.Parse(raw, True)
    except expat.ExpatError:
        pass


def parse(raw: bytes, source: Source) -> list[Item]:
    declared = ENCODING.search(raw[:200])
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in raw[:100] or (declared and declared.group(1).lower() not in (b"utf-8", b"us-ascii")):
        raise SourceError(f"{source.id}: only UTF-8 feeds are read (a wide encoding would hide a DOCTYPE from the check below)")
    _refuse_dtd(raw, source)
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
