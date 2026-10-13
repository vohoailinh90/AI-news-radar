"""Events -> ranked Events. score = topic weight + trust + recency + HN points + corroboration.
A pure function of the events and config/rules.yaml, so a ranking can be re-derived by hand."""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import PRIMARY, Event


def score_events(events: list[Event], rules: dict, now: datetime, points: dict[str, int] | None = None) -> list[Event]:
    cfg, points = rules["scoring"], points or {}
    weights = {spec["topic"]: 0 for spec in rules["topics"].values()}
    kind_weight = {name: spec["weight"] for name, spec in rules["topics"].items()}
    window = timedelta(hours=cfg["corroboration_window_hours"])
    for event in events:
        base = kind_weight.get(event.kind)
        if base is None:                              # snapshot-source kinds are not rule names
            base = {"benchmark-new-leader": 9, "benchmark-entry": 6, "free-model-added": 9, "model-became-free": 10,
                    "free-model-removed": 7, "price-drop": 9, "new-model": 7, "trending": 4}.get(event.kind, 5)
        age_hours = max((now - event.when).total_seconds() / 3600, 0)
        decay = 0.5 ** (age_hours / cfg["half_life_hours"])
        score = base + cfg["trust_bonus"][event.trust] + 4 * decay
        score += min(points.get(event.item_id, 0) / 100, cfg["hn_points_cap"])
        event.score = round(score, 3)
    for event in events:
        peers = [o for o in events if o is not event and o.vendor == event.vendor and o.topic == event.topic
                 and o.source != event.source and abs(o.when - event.when) <= window]
        distinct = {o.source for o in peers}
        if distinct:
            event.score = round(event.score + cfg["corroboration_bonus"] * len(distinct), 3)
            event.corroborated_by = sorted(o.source_name for o in peers if o.trust == PRIMARY and event.trust != PRIMARY)
    return sorted(events, key=lambda e: (-e.score, e.title))
