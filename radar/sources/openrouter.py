"""OpenRouter's public model list. Prices are per token as strings; we compare per 1M input tokens.
Diffed against the last snapshot: a new model, a model that became free (or stopped being), a price drop."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ..config import Source
from ..models import Event
from . import Context, SourceError, vendor_of_prefix

MAX_PER_KIND = 5


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
                                  "free": free, "created": int(row.get("created") or 0)}
    if not models:
        raise SourceError(f"{source.id}: the model list is empty")
    return models


def collect(source: Source, ctx: Context):
    current = snapshot_of(ctx.fetch(source.url, max_bytes=source.opt("max_bytes", 8_000_000)), source)
    previous = ctx.state.snapshots.get(source.id)
    ctx.state.snapshots[source.id] = current
    if previous is None:
        return [], []                              # baseline
    min_drop = float(source.opt("min_drop_pct", 20))
    groups: dict[str, list[tuple[tuple, Event]]] = {"free": [], "new": [], "drop": [], "gone": []}
    stamp = ctx.now.strftime("%Y%m%dT%H%M%S")      # one scan = one observation, so a repeat of an old transition is new

    def make(group, sort_key, kind, topic, model_id, entry, title, figures, when=None, suffix=""):
        # the id carries what makes this occurrence unique, so a second price drop or a second
        # "became free" for the same model is a new event rather than a repeat of the first
        event = Event(kind=kind, vendor=vendor_of_prefix(model_id, ctx.rules), topic=topic, title=title,
                      url=f"https://openrouter.ai/{model_id}", source=source.id, source_name=source.name,
                      trust=source.trust, when=when or ctx.now, item_id=f"{source.id}:{kind}:{model_id}{suffix}",
                      figures={"model": entry["name"], **figures})
        groups[group].append((sort_key, event))

    for model_id, now_entry in current.items():
        before = previous.get(model_id)
        created = datetime.fromtimestamp(now_entry["created"], timezone.utc) if now_entry["created"] else ctx.now
        newest_first = (-now_entry["created"], now_entry["name"])
        if before is None:
            if now_entry["free"]:
                make("free", newest_first, "free-model-added", "free", model_id, now_entry,
                     f"{now_entry['name']} is available free of charge on OpenRouter", {}, created)
            else:
                figs = {"input_price": per_million(now_entry["prompt"])} if now_entry["prompt"] else {}
                make("new", newest_first, "model-listed", "model", model_id, now_entry,
                     f"{now_entry['name']} is now listed on OpenRouter", figs, created)
        elif now_entry["free"] and not before.get("free"):
            make("free", newest_first, "model-became-free", "free", model_id, now_entry,
                 f"{now_entry['name']} became free of charge on OpenRouter", {}, suffix=f"@{stamp}")
        elif before.get("free") and not now_entry["free"]:
            make("gone", (0, now_entry["name"]), "model-no-longer-free", "free", model_id, now_entry,
                 f"{now_entry['name']} is no longer free on OpenRouter", {}, suffix=f"@{stamp}")
        elif before.get("prompt") and now_entry["prompt"] is not None and not now_entry["free"]:
            drop = (before["prompt"] - now_entry["prompt"]) / before["prompt"] * 100
            if drop >= min_drop:
                figs = {"percent": f"{round(drop)}", "old_price": per_million(before["prompt"]),
                        "new_price": per_million(now_entry["prompt"])}
                make("drop", (-drop, now_entry["name"]), "price-drop", "discount", model_id, now_entry,
                     f"{now_entry['name']} input price drops {figs['percent']}% on OpenRouter: "
                     f"${figs['old_price']} to ${figs['new_price']} per 1M tokens", figs, suffix=f"@{figs['old_price']}>{figs['new_price']}@{stamp}")
    for model_id, before in previous.items():
        if before.get("free") and model_id not in current:
            make("gone", (1, before["name"]), "free-model-removed", "free", model_id, before,
                 f"{before['name']} was removed from OpenRouter's free models", {}, suffix=f"@{stamp}")
    out: list[Event] = []
    for group in groups.values():
        out += [event for _, event in sorted(group, key=lambda pair: pair[0])[:MAX_PER_KIND]]
    return [], out
