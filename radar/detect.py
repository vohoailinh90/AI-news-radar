"""Items -> Events by rule. A rule matches an ACTION phrase from config/rules.yaml. Everything here is
deterministic; the same items and rules always give the same events."""

from __future__ import annotations

import re
from datetime import datetime

from .config import Source
from .models import COMMUNITY, Event, Item
from .textsafe import defang

PERCENT = re.compile(r"(\d{1,3})\s?%")


def compile_rules(rules: dict) -> dict:
    return {
        "vendors": {v: [re.compile(p, re.I) for p in pats] for v, pats in rules["vendors"].items()},
        "topics": {name: {**spec, "any": [re.compile(p, re.I) for p in spec.get("any", [])],
                          "boost": [re.compile(p, re.I) for p in spec.get("boost", [])],
                          "bullet_start": re.compile(spec["bullet_start"], re.I) if spec.get("bullet_start") else None}
                   for name, spec in rules["topics"].items()},
        "exclude": [re.compile(p, re.I) for p in rules.get("exclude", [])],
        "feature_skip": [re.compile(p) for p in rules.get("feature_skip", [])],
        "trending_points": int(rules["scoring"].get("trending_points", 300)),
    }


def vendor_for(text: str, fallback: str, compiled: dict) -> str:
    for vendor, patterns in compiled["vendors"].items():
        if any(p.search(text) for p in patterns):
            return vendor
    return fallback


def match_topic(text: str, kind: str, compiled: dict, *, bullet: bool = False):
    """The highest-weight topic whose rule matches `text`, as (name, spec) or None."""
    best = None
    for name, spec in compiled["topics"].items():
        if kind in spec.get("not_in_kinds", []) or ("only_in_kinds" in spec and kind not in spec["only_in_kinds"]):
            continue
        if spec["bullet_start"]:
            if not bullet or not spec["bullet_start"].search(text):
                continue
            if not any(p.search(text) for p in spec["boost"]) or any(p.search(text) for p in compiled["feature_skip"]):
                continue
        elif not any(p.search(text) for p in spec["any"]):
            continue
        if best is None or spec["weight"] > best[1]["weight"]:
            best = (name, spec)
    return best


def figures_for(text: str, topic: str) -> dict:
    found = PERCENT.search(text)
    if found and topic in ("discount", "quota"):
        return {"percent": found.group(1)}
    return {}


def detect(items: list[Item], sources: dict[str, Source], rules: dict, now: datetime) -> list[Event]:
    compiled = compile_rules(rules)
    events: list[Event] = []
    for item in items:
        source = sources[item.source]
        when = item.published or now
        if source.kind == "changelog_md":
            events += _from_changelog(item, source, compiled, when)
            continue
        text = f"{item.title}. {item.body[:600]}"
        if any(p.search(item.title) for p in compiled["exclude"]):
            continue
        hit = match_topic(text, source.kind, compiled)
        vendor = vendor_for(text, source.vendor, compiled)
        if hit:
            name, spec = hit
            events.append(_event(item, source, name, spec["topic"], vendor, defang(item.title), when,
                                 figures_for(text, spec["topic"])))
        elif source.trust == COMMUNITY and item.points >= compiled["trending_points"]:
            events.append(_event(item, source, "trending", "news", vendor, defang(item.title), when, {}))
    return events


def _event(item, source, kind, topic, vendor, title, when, figures) -> Event:
    return Event(kind=kind, vendor=vendor, topic=topic, title=title, url=item.url, source=source.id,
                 source_name=source.name, trust=source.trust, when=when, figures=figures, item_id=item.id)


def _from_changelog(item: Item, source: Source, compiled: dict, when: datetime) -> list[Event]:
    version, features, others = item.extra["version"], [], []
    for number, bullet in enumerate(item.extra["bullets"]):
        if any(p.search(bullet) for p in compiled["exclude"]):
            continue
        hit = match_topic(bullet, source.kind, compiled, bullet=True)
        if not hit:
            continue
        name, spec = hit
        event = _event(item, source, name, spec["topic"], vendor_for(bullet, source.vendor, compiled),
                       f"{source.name} {version}: {defang(bullet)}", when, {"version": version, **figures_for(bullet, spec["topic"])})
        event.item_id = f"{item.id}#{number}"
        boosts = sum(1 for p in compiled["topics"][name]["boost"] if p.search(bullet))
        (features if name == "new-feature" else others).append((boosts, number, event))
    features.sort(key=lambda t: (-t[0], t[1]))
    return [e for _, _, e in others] + [e for _, _, e in features[:3]]      # at most 3 features per version
