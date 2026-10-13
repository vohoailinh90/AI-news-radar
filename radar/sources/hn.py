"""Hacker News through the public Algolia API: always community-trust, a "what is hot" signal."""

from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import urlencode

from ..config import Source
from ..models import Item
from . import Context, SourceError, parse_time


def parse(raw: bytes, source: Source) -> list[Item]:
    try:
        hits = json.loads(raw).get("hits", [])
    except (ValueError, AttributeError) as exc:
        raise SourceError(f"{source.id}: not the JSON the Algolia API returns ({exc})") from exc
    items = []
    for hit in hits:
        if not isinstance(hit, dict) or not hit.get("title") or not hit.get("objectID"):
            continue
        discussion = f"https://news.ycombinator.com/item?id={hit['objectID']}"
        url = hit.get("url") if str(hit.get("url") or "").startswith("https://") else discussion
        items.append(Item(id=f"{source.id}:{hit['objectID']}", source=source.id, title=str(hit["title"]), url=url,
                          published=parse_time(hit.get("created_at")), points=int(hit.get("points") or 0),
                          extra={"comments": int(hit.get("num_comments") or 0), "discussion": discussion}))
    return items


def collect(source: Source, ctx: Context):
    since = int((ctx.now - timedelta(days=source.opt("max_age_days", 3))).timestamp())
    by_id: dict[str, Item] = {}
    for query in source.opt("queries", []):
        params = urlencode({"query": query, "tags": "story", "hitsPerPage": 20,
                            "numericFilters": f"points>={source.opt('min_points', 100)},created_at_i>{since}"})
        for item in parse(ctx.fetch(f"{source.url}?{params}"), source):
            by_id.setdefault(item.id, item)
    return list(by_id.values()), []
