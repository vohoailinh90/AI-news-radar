import json

import pytest

from radar.sources import SourceError, changelog, hn, leaderboard, openrouter, rss
from radar.store import State
from tests.helpers import NOW, context, fixture, source


def test_rss_real_anthropic_feed():
    items = rss.parse(fixture("anthropic_news_feed.xml"), source("anthropic-news"))
    assert len(items) == 20
    assert all(i.url.startswith("https://www.anthropic.com/") and i.published for i in items)
    assert items[0].title == "Building on our commitment to American scientific discovery"


def test_rss_drops_items_older_than_max_age(tmp_path):
    src = source("anthropic-news")
    ctx = context(tmp_path, {src.url: fixture("anthropic_news_feed.xml")})
    items, events = rss.collect(src, ctx)
    assert events == [] and 0 < len(items) < 20
    assert all((NOW - i.published).days <= src.opt("max_age_days") for i in items)


def test_atom_entries_are_parsed():
    items = rss.parse(fixture("synthetic_releases.atom"), source("codex-releases"))
    assert [i.title for i in items] == ["v1.2.0", "v1.1.0"]
    assert items[0].url == "https://github.com/acme/tool/releases/tag/v1.2.0"
    assert "Doubled the weekly usage limits" in items[0].body


def test_feed_with_doctype_or_entity_is_refused():
    evil = b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title></item></channel></rss>'
    with pytest.raises(SourceError, match="DOCTYPE"):
        rss.parse(evil, source("anthropic-news"))


def test_feed_with_only_a_doctype_is_refused_too():
    with pytest.raises(SourceError, match="DOCTYPE"):
        rss.parse(b'<?xml version="1.0"?><!DOCTYPE rss><rss><channel><item><title>x</title></item></channel></rss>', source("anthropic-news"))


def test_feed_that_is_not_xml_raises_source_error():
    with pytest.raises(SourceError):
        rss.parse(b"<html>not a feed", source("anthropic-news"))


def test_changelog_real_versions_and_state(tmp_path):
    sections = changelog.parse(fixture("claude_code_changelog_head.md").decode())
    assert sections[0][0] == "2.1.296" and len(sections) == 6 and len(sections[0][1]) > 10
    src = source("claude-code-changelog")
    url = {src.url: fixture("claude_code_changelog_head.md")}
    state = State(tmp_path / "s.json")
    first, _ = changelog.collect(src, context(tmp_path, url, state=state))
    assert [i.extra["version"] for i in first] == ["2.1.296"]          # first run: newest only
    again, _ = changelog.collect(src, context(tmp_path, url, state=state))
    assert again == []                                                  # nothing newer
    state.versions[src.id] = "2.1.294"
    newer, _ = changelog.collect(src, context(tmp_path, url, state=state))
    assert [i.extra["version"] for i in newer] == ["2.1.296", "2.1.295"]


def test_changelog_without_version_headings_is_a_source_error(tmp_path):
    src = source("claude-code-changelog")
    with pytest.raises(SourceError):
        changelog.collect(src, context(tmp_path, {src.url: b"# Changelog\n\nnothing here"}))


def test_leaderboard_baseline_emits_nothing_then_new_leader(tmp_path):
    from datetime import datetime, timezone
    src = source("aider-polyglot")
    raw = fixture("aider_polyglot_leaderboard.yml")
    entries = leaderboard.parse(raw, src)
    assert len(entries) == 69
    state = State(tmp_path / "s.json")
    now = datetime(2025, 8, 24, tzinfo=timezone.utc)
    _, baseline = leaderboard.collect(src, context(tmp_path, {src.url: raw}, now=now, state=state))
    assert baseline == []
    top = max(entries, key=lambda e: e["score"])
    rest = [e for e in entries if e["id"] != top["id"]]
    state.snapshots[src.id] = {"ids": sorted(e["id"] for e in rest), "best": max(e["score"] for e in rest)}
    _, events = leaderboard.collect(src, context(tmp_path, {src.url: raw}, now=now, state=state))
    assert [e.kind for e in events] == ["benchmark-new-leader"]
    assert events[0].figures["score"] == "88" and events[0].trust == "primary"


def test_leaderboard_ignores_back_filled_old_entries(tmp_path):
    src = source("aider-polyglot")
    raw = fixture("aider_polyglot_leaderboard.yml")
    state = State(tmp_path / "s.json")
    state.snapshots[src.id] = {"ids": [], "best": 0}
    _, events = leaderboard.collect(src, context(tmp_path, {src.url: raw}, state=state))   # NOW is a year later
    assert events == []


def _openrouter(tmp_path, before, after):
    src = source("openrouter-models")
    state = State(tmp_path / "s.json")
    leaderboard_ctx = lambda raw: context(tmp_path, {src.url: raw}, state=state)
    assert openrouter.collect(src, leaderboard_ctx(fixture(before)))[1] == []      # baseline
    return openrouter.collect(src, leaderboard_ctx(fixture(after)))[1]


def test_openrouter_diff_finds_free_new_and_price_drop(tmp_path):
    events = {(e.kind, e.figures["model"]): e for e in _openrouter(tmp_path, "synthetic_openrouter_before.json", "synthetic_openrouter_after.json")}
    assert ("free-model-added", "GPT New (free)") in events
    assert events[("free-model-added", "GPT New (free)")].vendor == "openai"
    assert ("model-became-free", "Acme Gamma") in events                  # 2.00 -> free
    assert ("free-model-removed", "Acme Old (free)") in events
    assert ("new-model", "Claude New") in events and events[("new-model", "Claude New")].figures["input_price"] == "3.00"
    drop = events[("price-drop", "Acme Alpha")]
    assert drop.figures == {"model": "Acme Alpha", "percent": "40", "old_price": "5.00", "new_price": "3.00"}
    assert ("price-drop", "Acme Beta") not in events                       # a 10% cut is under min_drop_pct
    assert not any(e.figures["model"] == "Acme Router" for e in events.values())   # "-1" price means unknown


def test_openrouter_bad_json_is_a_source_error(tmp_path):
    src = source("openrouter-models")
    with pytest.raises(SourceError):
        openrouter.collect(src, context(tmp_path, {src.url: b'{"oops": 1}'}))


def test_hn_parse_skips_empty_titles_and_falls_back_to_discussion_link():
    items = hn.parse(fixture("synthetic_hn_hits.json"), source("hn-ai"))
    assert [i.id for i in items] == ["hn-ai:9001", "hn-ai:9002"]
    assert items[1].url == "https://news.ycombinator.com/item?id=9002" and items[0].points == 450
