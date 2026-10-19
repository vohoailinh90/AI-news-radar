"""OpenRouter's public model list. Prices are per token as strings; we compare per 1M input tokens.
Diffed against the last snapshot: a new model, a model that became free (or stopped being), a price drop."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ..config import Source
from ..models import Event
from . import Context, SourceError, next_generation, occurrence, vendor_of_prefix


def price(value) -> float | None:
    """Per-token price as a float; None when unknown (OpenRouter uses "-1" for variable pricing)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def per_million(value: float) -> str:
    scaled = value * 1_000_000
    return f"{scaled:.2f}" if scaled >= 0.01 else f"{scaled:.4f}"      # never print $0.00 for a price that is not zero


def price_pair(old: float, new: float) -> tuple[str, str]:
    """per_million of both prices, with more digits while two different prices would print the same ("$0.02 to $0.02")."""
    pair = per_million(old), per_million(new)
    for digits in (4, 6, 8, 10):
        if pair[0] != pair[1]:
            break
        pair = f"{old * 1_000_000:.{digits}f}", f"{new * 1_000_000:.{digits}f}"
    return pair


def timestamp(value) -> int:
    """A creation time in seconds; 0 for anything that is not a plausible one (the row stays, its date is just unknown)."""
    try:
        seconds = int(value or 0)
        datetime.fromtimestamp(seconds, timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return 0
    return seconds


def snapshot_of(raw: bytes, source: Source) -> dict[str, dict]:
    try:
        rows = json.loads(raw)["data"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceError(f"{source.id}: not the model list JSON ({exc})") from exc
    models = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        pricing = row.get("pricing") or {}
        prompt, completion = price(pricing.get("prompt")), price(pricing.get("completion"))
        free = str(row["id"]).endswith(":free") or (prompt == 0 and completion == 0)
        models[str(row["id"])] = {"name": str(row.get("name") or row["id"]), "prompt": prompt,
                                  "free": free, "created": timestamp(row.get("created"))}
        if row.get("alias_target") or str(row["id"]).startswith("~"):
            models[str(row["id"])]["alias"] = True      # "~vendor/x-latest" redirects to whichever model is newest
    if not models:
        raise SourceError(f"{source.id}: the model list is empty")
    return models


def collect(source: Source, ctx: Context):
    current = snapshot_of(ctx.fetch(source.url, max_bytes=source.opt("max_bytes", 8_000_000)), source)
    previous = ctx.state.snapshots.get(source.id)
    taken = ctx.now.strftime("%Y%m%dT%H%M%S")
    if previous is None:
        ctx.state.snapshots[source.id] = {"taken": taken, "gen": 1, "models": current}
        return [], []                              # baseline
    # A transition's id names the snapshot it was measured against, not the time of this scan: if the run dies before
    # the state is saved, the replay measures against the same snapshot and gets the same ids (no double post), while
    # a genuine repeat (10 -> 5, back to 10, 5 again) is measured against a newer snapshot and is a new event.
    generation = next_generation(previous)
    previous, since = (previous["models"], occurrence(previous)) if "models" in previous else (previous, "legacy")
    min_drop = float(source.opt("min_drop_pct", 20))
    groups: dict[str, list[tuple[tuple, Event, str]]] = {"free": [], "new": [], "drop": [], "gone": []}

    def make(group, sort_key, kind, topic, model_id, entry, title, figures, detail="", suffix=""):
        # the id carries what makes this occurrence unique, so a second price drop or a second
        # "became free" for the same model is a new event rather than a repeat of the first
        event = Event(kind=kind, vendor=vendor_of_prefix(model_id, ctx.rules), topic=topic, title=title,
                      url=f"https://openrouter.ai/{model_id}", source=source.id, source_name=source.name,
                      trust=source.trust, when=ctx.now, detail=detail, item_id=f"{source.id}:{kind}:{model_id}{suffix}",
                      figures={"model": entry["name"], **figures})       # when = observation time: it is what the carry horizon ages
        groups[group].append((sort_key, event, model_id))

    for model_id, now_entry in current.items():
        before = previous.get(model_id)
        made = f"created {datetime.fromtimestamp(now_entry['created'], timezone.utc).date().isoformat()}" if now_entry["created"] else ""
        newest_first = (-now_entry["created"], now_entry["name"])
        if before is None:
            if now_entry["free"]:
                make("free", newest_first, "free-model-added", "free", model_id, now_entry,
                     f"{now_entry['name']} is available free of charge on OpenRouter", {}, detail=made, suffix=f"@{since}")
            else:
                figs = {"input_price": per_million(now_entry["prompt"])} if now_entry["prompt"] else {}
                make("new", newest_first, "model-listed", "model", model_id, now_entry,
                     f"{now_entry['name']} is now listed on OpenRouter", figs, detail=made, suffix=f"@{since}")
        elif now_entry["free"] and not before.get("free"):
            make("free", newest_first, "model-became-free", "free", model_id, now_entry,
                 f"{now_entry['name']} became free of charge on OpenRouter", {}, suffix=f"@{since}")
        elif before.get("free") and not now_entry["free"]:
            make("gone", (0, now_entry["name"]), "model-no-longer-free", "free", model_id, now_entry,
                 f"{now_entry['name']} is no longer free on OpenRouter", {}, suffix=f"@{since}")
        elif before.get("prompt") and now_entry["prompt"] is not None and not now_entry["free"] and not now_entry.get("alias"):
            # an alias takes the price of whatever it points to: that is the target model's own news, or a redirect, not a cut
            drop = (before["prompt"] - now_entry["prompt"]) / before["prompt"] * 100
            if drop >= min_drop:
                old_price, new_price = price_pair(before["prompt"], now_entry["prompt"])
                figs = {"percent": f"{round(drop)}", "old_price": old_price, "new_price": new_price}
                make("drop", (-drop, now_entry["name"]), "price-drop", "discount", model_id, now_entry,
                     f"{now_entry['name']} input price drops {figs['percent']}% on OpenRouter: "
                     f"${figs['old_price']} to ${figs['new_price']} per 1M tokens", figs, suffix=f"@{since}")
    for model_id, before in previous.items():
        if before.get("free") and model_id not in current:
            make("gone", (1, before["name"]), "free-model-removed", "free", model_id, before,
                 f"{before['name']} was removed from OpenRouter's free models", {}, suffix=f"@{since}")
    # Every transition seen is returned now and persisted by the scan before any "seen" mark: the exporter spreads a
    # burst over several issues (and keeps the rest pending), the collector never holds events back in its snapshot.
    out = [event for group in groups.values() for _, event, _ in sorted(group, key=lambda trio: trio[0])]
    ctx.state.snapshots[source.id] = {"taken": taken, "gen": generation, "models": current}       # last: a failure above leaves the old snapshot
    return [], out
