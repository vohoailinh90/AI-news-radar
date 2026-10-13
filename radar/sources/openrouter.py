"""OpenRouter's public model list. Prices are per token as strings; we compare per 1M input tokens.
Diffed against the last snapshot: a new model, a model that became free, a price drop."""

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
    return f"{value * 1_000_000:.2f}"


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
    events: dict[str, list[Event]] = {"free": [], "new": [], "drop": [], "gone": []}

    def make(kind, topic, model_id, entry, title, figures, when=None):
        return Event(kind=kind, vendor=vendor_of_prefix(model_id, ctx.rules), topic=topic, title=title,
                     url=f"https://openrouter.ai/{model_id}", source=source.id, source_name=source.name,
                     trust=source.trust, when=when or ctx.now, item_id=f"{source.id}:{kind}:{model_id}",
                     figures={"model": entry["name"], **figures})

    for model_id, now_entry in current.items():
        before = previous.get(model_id)
        created = datetime.fromtimestamp(now_entry["created"], timezone.utc) if now_entry["created"] else ctx.now
        if before is None:
            if now_entry["free"]:
                events["free"].append(make("free-model-added", "free", model_id, now_entry,
                                           f"{now_entry['name']} is available free of charge", {}, created))
            else:
                figs = {"input_price": per_million(now_entry["prompt"])} if now_entry["prompt"] else {}
                events["new"].append(make("new-model", "model", model_id, now_entry,
                                          f"{now_entry['name']} is now listed", figs, created))
        elif now_entry["free"] and not before.get("free"):
            events["free"].append(make("model-became-free", "free", model_id, now_entry,
                                       f"{now_entry['name']} became free of charge", {}))
        elif before.get("prompt") and now_entry["prompt"] is not None and not now_entry["free"]:
            drop = (before["prompt"] - now_entry["prompt"]) / before["prompt"] * 100
            if drop >= min_drop:
                figs = {"percent": f"{round(drop)}", "old_price": per_million(before["prompt"]),
                        "new_price": per_million(now_entry["prompt"])}
                events["drop"].append(make("price-drop", "discount", model_id, now_entry,
                                           f"{now_entry['name']} input price drops {figs['percent']}%: "
                                           f"${figs['old_price']} to ${figs['new_price']} per 1M tokens", figs))
    for model_id, before in previous.items():
        if before.get("free") and model_id not in current:
            events["gone"].append(make("free-model-removed", "free", model_id, before,
                                       f"{before['name']} is no longer listed as free", {}))
    out: list[Event] = []
    for group in events.values():
        group.sort(key=lambda e: (-(float(e.figures.get("percent", 0))), e.title))
        out += group[:MAX_PER_KIND]
    return [], out
