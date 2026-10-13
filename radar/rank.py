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


def tokens(title: str) -> set[str]:
    """Distinctive words of a headline: not generic, not the vendor's own name (every Claude story has that)."""
    words = re.findall(r"[a-z0-9][a-z0-9.\-]*", title.lower())
    return {w for w in words if w not in STOP and w not in VENDOR_WORDS and (len(w) >= 4 or any(c.isdigit() for c in w))}


def same_story(a: Event, b: Event) -> bool:
    """Two events corroborate each other only when they name the same vendor and topic AND share at least two
    distinctive words. Vendor "other" never corroborates: it matches anything."""
    return (a.vendor == b.vendor != "other" and a.topic == b.topic and a.source != b.source
            and len(tokens(a.title) & tokens(b.title)) >= 2)


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
                    "trending": 4}.get(event.kind, 5)
        if event.vendor == "other":
            base -= cfg.get("unknown_vendor_penalty", {}).get(event.kind, 0)
        age_hours = max((now - event.when).total_seconds() / 3600, 0)
        decay = 0.5 ** (age_hours / cfg["half_life_hours"])
        score = base + cfg["trust_bonus"][event.trust] + 4 * decay
        score += min(points.get(event.item_id, 0) / 100, cfg["hn_points_cap"])
        event.score = round(score, 3)
    for event in events:
        peers = [o for o in events if o is not event and same_story(event, o) and abs(o.when - event.when) <= window]
        distinct = {o.source for o in peers}
        if distinct:
            event.score = round(event.score + cfg["corroboration_bonus"] * len(distinct), 3)
            event.corroborated_by = sorted(o.source_name for o in peers if o.trust == PRIMARY and event.trust != PRIMARY)
    return sorted(events, key=lambda e: (-e.score, e.title))
