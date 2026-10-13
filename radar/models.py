"""The three shapes that travel through the pipeline: Item (collected) -> Event (detected) -> Story (exported)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

PRIMARY, COMMUNITY = "primary", "community"
TRUSTS = (PRIMARY, COMMUNITY)


@dataclass
class Item:
    """One thing a source said. `extra` carries structured fields for snapshot sources."""
    id: str
    source: str
    title: str
    url: str
    published: datetime | None = None
    body: str = ""
    points: int = 0
    extra: dict = field(default_factory=dict)


@dataclass
class Event:
    """A thing worth posting about. `figures` holds every number a post may state, as strings,
    so the exporter writes the same strings into the brief and into the posts."""
    kind: str
    vendor: str
    topic: str
    title: str
    url: str
    source: str
    source_name: str
    trust: str
    when: datetime
    detail: str = ""
    figures: dict = field(default_factory=dict)
    item_id: str = ""
    score: float = 0.0
    corroborated_by: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        """True only when a primary source states it, or a primary source corroborates a community one."""
        return self.trust == PRIMARY or bool(self.corroborated_by)


@dataclass
class SourceReport:
    """How one source did in one run. A dead source is reported, never fatal."""
    id: str
    ok: bool
    items: int = 0
    note: str = ""
