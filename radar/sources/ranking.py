"""A ranking board: a public leaderboard that scores agent + model settings and publishes what a run costs
(Terminal-Bench 4.0). Unlike the other snapshot sources, the ranking itself is the news, so the FIRST scan reports it
too instead of recording a baseline.

The story is one comparison, computed from the board's own numbers: the top row, and the "value pick", the cheapest
row that still reaches `value_min_share` of the top score for at most `value_max_cost_share` of the top row's cost.
A board where no row qualifies produces no story. After the first story the board is reported again only when its
top row or its value pick CHANGES (the fingerprint names the two rows, never the numbers, so a rerun that moves a
score by a point is not news), and not within `cooldown_days` of the last story.

A board whose newest row is older than `max_age_days` is reported as stale and produces nothing: a ranking is read
as "now". Every row is an agent + model + setting together, never a model alone, and the label says so."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date

from ..config import Source
from ..models import Event
from . import Context, SourceError, next_generation, occurrence, parse_time

PUSH = re.compile(r'self\.__next_f\.push\(\[1,(".*?")\]\)</script>', re.S)


def flight_text(raw: bytes, source: Source) -> str:
    """The text a Next.js page streams to its own front end (`self.__next_f.push([1,"..."])`), decoded and joined."""
    html = raw.decode("utf-8", errors="replace")
    parts = []
    for chunk in PUSH.findall(html):
        try:
            parts.append(json.loads(chunk))
        except ValueError:
            continue                                  # a chunk that does not decode is skipped; the row check decides
    if not parts:
        raise SourceError(f"{source.id}: no page data found; the page format changed")
    return "".join(parts)


def parse_tbench(raw: bytes, source: Source) -> tuple[list[dict], date | None]:
    """Terminal-Bench 4.0 leaderboard page -> rows {label, score (%), cost (USD per trial)} and the board's newest update.
    The cost of a row is the board's total cost for the row divided by its trials: a row without either is dropped."""
    text, decoder = flight_text(raw, source), json.JSONDecoder()
    found = None
    for match in re.finditer(r'"rows":\[', text):
        try:
            candidate, _ = decoder.raw_decode(text[match.end() - 1:])
        except ValueError:
            continue
        if isinstance(candidate, list) and candidate and all(isinstance(r, dict) and "metrics" in r for r in candidate):
            found = candidate
            break
    if found is None:
        raise SourceError(f"{source.id}: no leaderboard rows found; the page format changed")
    rows, stamps = [], []
    for entry in found:
        try:
            if entry.get("status", "display") != "display":
                continue
            meta, metrics = entry["metadata"], entry["metrics"]
            trials = int(metrics.get("n_trials") or entry.get("n_trials"))
            cost = float(metrics["total_cost_usd"]) / trials
            effort = str(meta.get("reasoning_effort") or "")
            label = f"{meta['agent_display']['label']} + {meta['model_display']['label']}" + (f" ({effort})" if effort and effort != "none" else "")
            rows.append({"label": label, "score": float(metrics["accuracy"]), "cost": cost})
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue                                  # no cost or no trials: the row cannot be compared
        stamp = parse_time(entry.get("updated_at"))
        if stamp:
            stamps.append(stamp.date())
    return rows, max(stamps, default=None)


FORMATS = {"tbench_leaderboard": parse_tbench}


def usable(rows: list[dict], source: Source) -> list[dict]:
    """Rows with a real score and a positive cost. Too few means the page changed shape, not that the board is small."""
    good = [r for r in rows if all(math.isfinite(r[k]) for k in ("score", "cost")) and 0 <= r["score"] <= 100 and r["cost"] > 0]
    if len(good) < int(source.opt("min_rows", 10)):
        raise SourceError(f"{source.id}: only {len(good)} usable row(s); the page format changed")
    return good


def pick(rows: list[dict], min_share: float, max_cost_share: float) -> tuple[dict, dict | None]:
    """(top row, value pick). The top row is the best score, the cheaper one on a tie. The value pick is the cheapest
    other row at or above `min_share` of the top score; it only counts when it also costs at most `max_cost_share` of the top."""
    top = min(rows, key=lambda r: (-r["score"], r["cost"], r["label"]))
    close = [r for r in rows if r is not top and r["score"] >= top["score"] * min_share]
    value = min(close, key=lambda r: (r["cost"], -r["score"], r["label"]), default=None)
    if value is None or value["cost"] > top["cost"] * max_cost_share:
        return top, None
    return top, value


def money(amount: float) -> str:
    return f"{amount:.2f}" if amount >= 0.01 else f"{amount:.4f}"          # never print $0.00 for a cost that is not zero


def percent(part: float, whole: float) -> str:
    share = part / whole * 100
    return "<1" if share < 1 else f"{round(share)}"


def fingerprint(top: dict, value: dict) -> str:
    return hashlib.sha1(f"{top['label']}\n{value['label']}".encode("utf-8")).hexdigest()[:12]


def collect(source: Source, ctx: Context):
    parser = FORMATS.get(source.opt("format"))
    if parser is None:
        raise SourceError(f"{source.id}: unknown ranking format {source.opt('format')!r}")
    rows, as_of = parser(ctx.fetch(source.url, max_bytes=source.opt("max_bytes", 3_000_000)), source)
    rows = usable(rows, source)
    today = ctx.now.date()
    max_age = int(source.opt("max_age_days", 45))
    if as_of and (today - as_of).days > max_age:
        ctx.notes[source.id] = f"board last updated {as_of.isoformat()} — STALE ({(today - as_of).days} days old), no story"
        return [], []
    top, value = pick(rows, float(source.opt("value_min_share", 0.85)), float(source.opt("value_max_cost_share", 0.5)))
    if value is None:
        ctx.notes[source.id] = f"{len(rows)} rows, top {top['label']}; no row within the value thresholds, no story"
        return [], []
    pair = fingerprint(top, value)
    previous = ctx.state.snapshots.get(source.id)
    if isinstance(previous, dict) and previous.get("fingerprint") == pair:
        ctx.notes[source.id] = f"{len(rows)} rows, same top and value pick as the last story, no story"
        return [], []
    if isinstance(previous, dict):
        try:
            waited = (today - date.fromisoformat(str(previous.get("announced")))).days
        except ValueError:
            waited = None                              # no usable date on the old snapshot: it does not hold anything back
        cooldown = int(source.opt("cooldown_days", 14))
        if waited is not None and waited < cooldown:
            ctx.notes[source.id] = f"top or value pick changed, last story {waited} day(s) ago (cooldown {cooldown}), no story yet"
            return [], []
    unit, suffix = str(source.opt("cost_unit", "task")), str(source.opt("score_unit", ""))

    def shown(row: dict) -> str:
        return f"{row['score']:.1f}{suffix}"

    figures = {"model": value["label"], "benchmark": str(source.opt("benchmark", source.name)),
               "value_score": shown(value), "value_cost": money(value["cost"]),
               "top_model": top["label"], "top_score": shown(top), "top_cost": money(top["cost"]),
               "pct_score": percent(value["score"], top["score"]), "pct_cost": percent(value["cost"], top["cost"]), "unit": unit}
    title = (f"{figures['benchmark']}: {value['label']} scores {figures['value_score']}, {figures['pct_score']}% of the top score "
             f"({top['label']}, {figures['top_score']}) for {figures['pct_cost']}% of the cost "
             f"(${figures['value_cost']} vs ${figures['top_cost']} per {unit})")
    # the id names the snapshot this story was measured against (see openrouter.collect): a replay of an unsaved scan
    # gets the same id and is not posted twice, while a later change back to an earlier pair is a new story
    since = occurrence(previous) if isinstance(previous, dict) else "g0"
    event = Event(kind="ranking-board", vendor="other", topic="ranking", title=title, url=str(source.opt("page_url", source.url)),
                  source=source.id, source_name=source.name, trust=source.trust, when=ctx.now,
                  detail=f"board updated {as_of.isoformat()}" if as_of else "", figures=figures,
                  item_id=f"{source.id}:ranking:{pair}@{since}")
    ctx.state.snapshots[source.id] = {"fingerprint": pair, "top": top["label"], "value": value["label"],
                                      "announced": today.isoformat(), "taken": ctx.now.strftime("%Y%m%dT%H%M%S"),
                                      "gen": next_generation(previous)}               # last: a failure above leaves the old snapshot
    ctx.notes[source.id] = f"{len(rows)} rows, top {top['label']}, value pick {value['label']}"
    return [], [event]
