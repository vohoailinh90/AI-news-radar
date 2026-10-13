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


def vendor_of_prefix(model_id: str, rules: dict) -> str:
    return rules.get("vendor_prefix", {}).get(model_id.split("/", 1)[0], "other")
