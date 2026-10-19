import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCES = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))["sources"]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_every_source_says_when_it_was_last_fetched_live():
    for src in SOURCES:
        value = src.get("live_checked")
        assert value is False or (isinstance(value, str) and datetime.date.fromisoformat(value) <= datetime.date.today()), src["id"]


def test_readme_shows_the_not_run_marker_only_while_a_source_is_unchecked():
    unchecked = [s["id"] for s in SOURCES if s.get("live_checked") is False]
    assert ("❌" in README) == bool(unchecked), f"README marker and live_checked disagree; unchecked sources: {unchecked}"
