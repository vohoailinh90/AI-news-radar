"""data/state.json: what earlier runs saw, so a run reports only what is new.

First run is a baseline for snapshot sources (OpenRouter prices, leaderboard entries): there is
nothing to diff against, so they record and emit nothing rather than flooding the first digest."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEEN_DAYS = 120


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class State:
    def __init__(self, path: Path, data: dict | None = None):
        self.path = path
        data = data or {}
        self.seen: dict[str, str] = dict(data.get("seen", {}))          # item id -> ISO time first seen
        self.versions: dict[str, str] = dict(data.get("versions", {}))  # changelog source -> newest version seen
        self.snapshots: dict[str, dict] = dict(data.get("snapshots", {}))
        self.last_run: str = data.get("last_run", "")

    @classmethod
    def load(cls, path: Path) -> "State":
        try:
            return cls(path, json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return cls(path)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{path} is unreadable ({exc}); fix or delete it — a silent reset would re-report old news") from exc

    def mark_seen(self, item_id: str, now: datetime) -> None:
        self.seen.setdefault(item_id, now.isoformat())

    def prune(self, now: datetime) -> None:
        cutoff = (now - timedelta(days=SEEN_DAYS)).isoformat()
        self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}

    def save(self, now: datetime) -> None:
        self.prune(now)
        self.last_run = now.isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"last_run": self.last_run, "seen": self.seen, "versions": self.versions, "snapshots": self.snapshots}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.path)       # atomic: a crash never leaves half a state file
