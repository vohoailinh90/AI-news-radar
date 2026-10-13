"""config/sources.yaml and config/rules.yaml, loaded and validated once."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .models import TRUSTS

ROOT = Path(__file__).resolve().parent.parent
KINDS = ("rss", "changelog_md", "yaml_leaderboard", "openrouter", "hn")


class ConfigError(ValueError):
    pass


@dataclass
class Source:
    id: str
    kind: str
    url: str
    name: str
    vendor: str
    trust: str
    options: dict
    enabled: bool = True

    def opt(self, key: str, default=None):
        return self.options.get(key, default)


def _load(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping")
    return data


def load_sources(root: Path = ROOT) -> list[Source]:
    raw = _load(root / "config" / "sources.yaml").get("sources")
    if not isinstance(raw, list) or not raw:
        raise ConfigError("config/sources.yaml: 'sources' must be a non-empty list")
    sources, seen = [], set()
    for entry in raw:
        missing = [k for k in ("id", "kind", "url", "name", "trust") if not entry.get(k)]
        if missing:
            raise ConfigError(f"config/sources.yaml: a source is missing {', '.join(missing)}: {entry}")
        if entry["kind"] not in KINDS:
            raise ConfigError(f"source {entry['id']}: kind must be one of {', '.join(KINDS)}")
        if entry["trust"] not in TRUSTS:
            raise ConfigError(f"source {entry['id']}: trust must be one of {', '.join(TRUSTS)}")
        if entry["id"] in seen:
            raise ConfigError(f"duplicate source id {entry['id']}")
        seen.add(entry["id"])
        if not str(entry["url"]).startswith("https://"):
            raise ConfigError(f"source {entry['id']}: url must be https://")
        known = {"id", "kind", "url", "name", "vendor", "trust", "enabled"}
        sources.append(Source(id=entry["id"], kind=entry["kind"], url=entry["url"], name=str(entry["name"]),
                              vendor=str(entry.get("vendor") or "other"), trust=entry["trust"],
                              enabled=bool(entry.get("enabled", True)),
                              options={k: v for k, v in entry.items() if k not in known}))
    return sources


def load_rules(root: Path = ROOT) -> dict:
    rules = _load(root / "config" / "rules.yaml")
    for key in ("vendors", "topics", "scoring", "export"):
        if key not in rules:
            raise ConfigError(f"config/rules.yaml: missing '{key}'")
    return rules
