"""One module per source kind. Each `collect(source, ctx)` returns (items, events):
text sources return Items for detect.py to read; snapshot sources diff against the saved
snapshot and return exact Events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

from ..models import Event, Item
from ..store import State


@dataclass
class Context:
    fetch: Callable[..., bytes]
    state: State
    now: datetime
    rules: dict


class SourceError(RuntimeError):
    pass


def parse_time(text: str | None) -> datetime | None:
    """RFC 822 (RSS) or ISO 8601 (Atom, JSON) -> aware UTC, or None when it is not a date."""
    if not text:
        return None
    text = text.strip()
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def occurrence(previous) -> str:
    """The name of the snapshot an observation is measured against: a monotonic generation, never a wall-clock time
    (two scans in the same second, or with the same supplied `now`, must not share an occurrence)."""
    return f"g{previous['gen']}" if isinstance(previous, dict) and isinstance(previous.get("gen"), int) else "legacy"


def next_generation(previous) -> int:
    return previous["gen"] + 1 if isinstance(previous, dict) and isinstance(previous.get("gen"), int) else 1


def vendor_of_prefix(model_id: str, rules: dict) -> str:
    return rules.get("vendor_prefix", {}).get(model_id.split("/", 1)[0], "other")
