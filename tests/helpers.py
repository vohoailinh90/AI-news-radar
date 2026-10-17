"""Shared test plumbing: fixtures, a temp repo root with the real config, a fake fetcher."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from radar.config import ROOT, load_rules, load_sources
from radar.http import FetchError
from radar.sources import Context
from radar.store import State

FIXTURES = ROOT / "tests" / "fixtures"
NOW = datetime(2026, 10, 13, 22, 0, tzinfo=timezone.utc)
REAL = {"anthropic-news": "anthropic_news_feed.xml", "claude-code-changelog": "claude_code_changelog_head.md",
        "aider-polyglot": "aider_polyglot_leaderboard.yml", "terminal-bench-4": "tbench_leaderboard_head.html"}


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def source(source_id: str):
    return next(s for s in load_sources() if s.id == source_id)


def make_root(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    return tmp_path


def fetcher_for(mapping: dict[str, str]):
    """source id -> fixture file; every other source raises FetchError like a blocked host would."""
    by_url = {s.url: s.id for s in load_sources()}

    def fetch(url, **kwargs):
        sid = by_url.get(url.split("?")[0])
        if sid in mapping:
            return fixture(mapping[sid])
        raise FetchError(f"{url}: blocked")
    return fetch


def context(tmp_path: Path, fetch_map: dict[str, bytes] | None = None, now=NOW, state=None) -> Context:
    def fetch(url, **kwargs):
        return (fetch_map or {})[url.split("?")[0]]
    return Context(fetch=fetch, state=state or State(tmp_path / "state.json"), now=now, rules=load_rules())
