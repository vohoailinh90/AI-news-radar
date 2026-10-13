"""A benchmark leaderboard published as a YAML list (Aider polyglot). Diffed against the ids seen
before: a new entry is news; a new entry that beats the best score so far is bigger news.
The first run only records. A source whose newest entry is old is reported as stale."""

from __future__ import annotations

from datetime import timedelta, timezone, datetime

import yaml

from ..config import Source
from ..models import COMMUNITY, Event
from . import Context, SourceError, parse_time


def parse(raw: bytes, source: Source) -> list[dict]:
    fields = source.opt("fields", {})
    try:
        rows = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise SourceError(f"{source.id}: not valid YAML ({exc})") from exc
    if not isinstance(rows, list):
        raise SourceError(f"{source.id}: expected a YAML list of entries")
    entries = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            score = float(row[fields["score"]])
        except (KeyError, TypeError, ValueError):
            continue                               # an entry without a score cannot be ranked
        stamp = row.get(fields.get("date", "date"))
        entries.append({"id": str(row.get(fields["id"]) or row.get(fields["name"])), "name": str(row.get(fields["name"])),
                        "score": score, "date": str(stamp) if stamp else "", "cost": row.get(fields.get("cost", "cost"))})
    if not entries:
        raise SourceError(f"{source.id}: no scored entries; the file format changed")
    return entries


def collect(source: Source, ctx: Context):
    entries = parse(ctx.fetch(source.url, max_bytes=source.opt("max_bytes", 5_000_000)), source)
    previous = ctx.state.snapshots.get(source.id)
    best = max(e["score"] for e in entries)
    newest = max((d for d in (parse_time(e["date"]) for e in entries) if d), default=None)
    ctx.state.snapshots[source.id] = {"ids": sorted(e["id"] for e in entries), "best": best,
                                      "newest": newest.date().isoformat() if newest else ""}
    if previous is None:
        return [], []                              # baseline
    known, old_best = set(previous["ids"]), float(previous["best"])
    events = []
    for entry in sorted((e for e in entries if e["id"] not in known), key=lambda e: -e["score"]):
        when = parse_time(entry["date"]) or ctx.now
        if when < ctx.now - timedelta(days=source.opt("max_age_days", 30)):
            continue                               # a back-filled old entry is not news
        leader = entry["score"] > old_best
        events.append(Event(
            kind="benchmark-new-leader" if leader else "benchmark-entry", vendor="other", topic="benchmark",
            title=f"{entry['name']} scores {entry['score']:g}% on {source.opt('benchmark', source.name)}"
                  + (", a new top score" if leader else ""),
            url=source.opt("page_url", "https://aider.chat/docs/leaderboards/"), source=source.id,
            source_name=source.name, trust=source.trust, when=when, item_id=f"{source.id}:{entry['id']}",
            figures={"score": f"{entry['score']:g}", "model": entry["name"], "benchmark": source.opt("benchmark", source.name)}))
        old_best = max(old_best, entry["score"])
    return [], events
