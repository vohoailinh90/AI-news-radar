"""Events -> ranked Events. score = topic weight + trust + recency + HN points + corroboration.
A pure function of the events and config/rules.yaml, so a ranking can be re-derived by hand."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .models import PRIMARY, Event

VENDOR_WORDS = {"claude", "anthropic", "openai", "codex", "chatgpt", "gemini", "google", "deepseek", "qwen", "llama",
                "meta", "grok", "xai", "mistral", "openrouter"}
STOP = {"the", "and", "for", "with", "new", "now", "from", "that", "this", "have", "been", "will", "your", "all", "are",
        "introducing", "announcing", "released", "release", "releases", "launches", "launched", "available", "plans",
        "users", "into", "over", "more", "show", "just", "than", "about", "after", "before", "model", "models"}


TIERS = {"pro", "max", "mini", "flash", "opus", "sonnet", "haiku", "nano", "lite", "oss", "ultra"}
DIRECTIONS = {
    "up": re.compile(r"\b(?:increas\w*|doubl\w*|rais\w*|extend\w*|expand\w*|boost\w*|bump\w*|extra|higher|more|\d+x)\b", re.I),
    "down": re.compile(r"\b(?:cut|cuts|reduc\w*|lower\w*|tighten\w*|less|cheaper|drops?|dropped)\b", re.I),
    "reset": re.compile(r"\b(?:reset\w*|refresh\w*)\b", re.I),
    "end": re.compile(r"\b(?:no longer|ends?|ended|ending|removed|discontinu\w*|sunset\w*)\b", re.I),
}


def tokens(title: str) -> set[str]:
    """Distinctive words of a headline: not generic, not the vendor's own name (every Claude story has that)."""
    words = re.findall(r"[a-z0-9][a-z0-9.\-]*", title.lower())
    return {w for w in words if w not in STOP and w not in VENDOR_WORDS and (len(w) >= 4 or any(c.isdigit() for c in w) or w in TIERS)}


def directions(title: str) -> set[str]:
    return {name for name, pattern in DIRECTIONS.items() if pattern.search(title)}


def same_story(a: Event, b: Event) -> bool:
    """Two events corroborate each other only when they name the same vendor and topic, share at least two
    distinctive words, and do not point opposite ways ("limits doubled" never confirms "limits cut"; a story
    that something ended never confirms one that it began). Vendor "other" matches anything, so it never counts."""
    if not (a.vendor == b.vendor != "other" and a.topic == b.topic and a.source != b.source):
        return False
    if len(tokens(a.title) & tokens(b.title)) < 2:
        return False
    da, db = directions(a.title), directions(b.title)
    if ("end" in da) != ("end" in db):
        return False
    if a.topic in ("quota", "discount") and not (da & db):
        return False                                     # the commercial topics need the same direction on both sides
    return True


def corroborators(event: Event, events: list[Event], window: timedelta, newest: datetime | None = None) -> list[str]:
    """The primary sources that confirm a community `event`: same story, within `window` of it. When `newest` (the latest report of
    the story the event is exported in) is given, a source older than `window` before it no longer counts: it belongs to an earlier
    telling of the story, and its confirmation must not carry over to a recurrence."""
    if event.trust == PRIMARY:
        return []
    return sorted(o.source_name for o in events
                  if o is not event and o.trust == PRIMARY and same_story(event, o) and abs(o.when - event.when) <= window
                  and (newest is None or newest - o.when <= window))


def score_events(events: list[Event], rules: dict, now: datetime, points: dict[str, int] | None = None) -> list[Event]:
    cfg, points = rules["scoring"], points or {}
    kind_weight = {name: spec["weight"] for name, spec in rules["topics"].items()}
    window = timedelta(hours=cfg["corroboration_window_hours"])
    for event in events:
        event.corroborated_by = []
        base = kind_weight.get(event.kind)
        if base is None:                              # snapshot-source kinds are not rule names
            base = {"benchmark-new-leader": 9, "benchmark-entry": 6, "free-model-added": 9, "model-became-free": 10,
                    "model-no-longer-free": 7, "free-model-removed": 6, "price-drop": 9, "model-listed": 7,
                    "ranking-board": 8, "trending": 4}.get(event.kind, 5)
        if event.vendor == "other":
            base -= cfg.get("unknown_vendor_penalty", {}).get(event.kind, 0)
        age_hours = max((now - event.when).total_seconds() / 3600, 0)
        decay = 0.5 ** (age_hours / cfg["half_life_hours"])
        score = base + cfg["trust_bonus"][event.trust] + 4 * decay
        score += min(max(event.points, points.get(event.item_id, 0)) / 100, cfg["hn_points_cap"])
        event.score = round(score, 3)
    for event in events:
        peers = [o for o in events if o is not event and same_story(event, o) and abs(o.when - event.when) <= window]
        distinct = {o.source for o in peers}
        if distinct:
            event.score = round(event.score + cfg["corroboration_bonus"] * len(distinct), 3)
            event.corroborated_by = corroborators(event, events, window)
    return sorted(events, key=lambda e: (-e.score, -cut_percent(e), e.title))


def cut_percent(event: Event) -> int:
    """The size of a price cut. Fresh price cuts all score the same, so without this the tie falls to the title and
    a vendor whose name sorts first (DeepSeek) takes every spotlight regardless of how big its cut is."""
    try:
        return int(event.figures["percent"]) if event.kind == "price-drop" else 0
    except (KeyError, TypeError, ValueError):
        return 0
