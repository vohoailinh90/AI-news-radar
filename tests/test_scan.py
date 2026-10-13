import argparse
import os
from pathlib import Path

import pytest

from radar import cli
from radar.models import SourceReport
from tests.helpers import NOW, REAL, fetcher_for, make_root

STUDIO = Path(os.environ.get("STUDIO_PATH", "/home/user/tool-marketing-studio"))


def run_scan(root, mapping=REAL):
    return cli.scan(root, fetcher=fetcher_for(mapping), now=NOW)


def test_a_dead_source_is_reported_and_never_fatal(tmp_path):
    root = make_root(tmp_path)
    events, reports = run_scan(root)
    status = {r.id: r.ok for r in reports}
    assert status["anthropic-news"] and status["claude-code-changelog"] and not status["openrouter-models"] and not status["hn-ai"]
    assert events and (root / "data" / "state.json").exists()
    stale = next(r for r in reports if r.id == "aider-polyglot")
    assert "STALE" in stale.note                                    # the real Aider file is a year old: say so


def test_second_scan_of_the_same_data_reports_nothing_twice(tmp_path):
    root = make_root(tmp_path)
    first, _ = run_scan(root)
    cli.save_scan(root, first, [], NOW)
    second, _ = run_scan(root)
    ids = [e.item_id for e in second]
    assert first and len(ids) == len(set(ids)) and set(ids) == {e.item_id for e in first}   # carried over, never duplicated


def test_when_every_source_fails_state_is_left_untouched(tmp_path):
    root = make_root(tmp_path)
    events, reports = run_scan(root, mapping={})
    assert events == [] and not any(r.ok for r in reports)
    assert not (root / "data" / "state.json").exists()               # a lost baseline would re-report old news


def test_scan_command_exits_2_when_every_source_fails(tmp_path, monkeypatch, capsys):
    from radar.http import FetchError
    root = make_root(tmp_path)
    monkeypatch.setattr(cli, "fetch", lambda url, **k: (_ for _ in ()).throw(FetchError(f"{url}: down")))
    assert cli.cmd_scan(argparse.Namespace(), root) == 2
    assert "every source failed" in capsys.readouterr().err


def test_a_parser_bug_in_one_source_does_not_hide_the_others(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    monkeypatch.setattr(cli.COLLECTORS["rss"], "collect", lambda *a, **k: (_ for _ in ()).throw(KeyError("boom")))
    events, reports = run_scan(root)
    broken = next(r for r in reports if r.id == "anthropic-news")
    assert not broken.ok and "internal error" in broken.note
    assert any(r.ok for r in reports if r.id == "claude-code-changelog")


def test_corrupt_state_stops_the_run_instead_of_resetting(tmp_path):
    root = make_root(tmp_path)
    (root / "data").mkdir()
    (root / "data" / "state.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unreadable"):
        run_scan(root)


def test_export_command_end_to_end_and_exit_codes(tmp_path):
    root = make_root(tmp_path)
    events, reports = run_scan(root)
    cli.save_scan(root, events, reports, NOW)
    args = argparse.Namespace(date="2026-10-13", studio=None, force=False)
    assert cli.cmd_export(args, root) == 0
    assert (root / "out" / "ai-news-2026-10-13" / "REPORT.md").exists()
    cli.save_scan(root, events, reports, NOW)                         # an operator exporting the same scan again
    assert cli.cmd_export(args, root) == 4                           # second export of the same issue is refused
    assert cli.cmd_export(argparse.Namespace(date="2026-10-13", studio=None, force=True), root) == 0
    empty = make_root(tmp_path / "empty")
    cli.save_scan(empty, [], [SourceReport("x", True)], NOW)
    assert cli.cmd_export(argparse.Namespace(date="2026-10-13", studio=None, force=False), empty) == 3


@pytest.mark.skipif(not (STUDIO / "mkt" / "check.py").exists(), reason="needs a tool-marketing-studio checkout (STUDIO_PATH)")
def test_exported_bundle_passes_the_studios_own_check(tmp_path, capsys):
    root = make_root(tmp_path)
    events, reports = run_scan(root)
    cli.save_scan(root, events, reports, NOW)
    cli.cmd_export(argparse.Namespace(date="2026-10-13", studio=None, force=False), root)
    capsys.readouterr()
    code = cli.cmd_verify(argparse.Namespace(studio=str(STUDIO), slug="ai-news-2026-10-13"), root)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "0 error(s)" in out


@pytest.mark.skipif(not (STUDIO / "mkt" / "check.py").exists(), reason="needs a tool-marketing-studio checkout (STUDIO_PATH)")
def test_every_kind_of_event_passes_the_studios_check_in_all_three_languages(tmp_path, capsys):
    """OpenRouter diff + a Hacker News rumour + release notes + the real changelog and feed, all in one issue."""
    import json
    from radar.sources import openrouter, hn, rss
    from radar.detect import detect
    from radar.config import load_rules, load_sources
    from radar.rank import score_events
    from radar.store import State
    from tests.helpers import context, fixture, source

    rules, sources = load_rules(), {s.id: s for s in load_sources()}
    state, orr = State(tmp_path / "s.json"), source("openrouter-models")
    openrouter.collect(orr, context(tmp_path, {orr.url: fixture("synthetic_openrouter_before.json")}, state=state))
    events = openrouter.collect(orr, context(tmp_path, {orr.url: fixture("synthetic_openrouter_after.json")}, state=state))[1]
    items = hn.parse(fixture("synthetic_hn_hits.json"), source("hn-ai")) + rss.parse(fixture("synthetic_releases.atom"), source("codex-releases"))
    items += rss.parse(fixture("anthropic_news_feed.xml"), source("anthropic-news"))[:3]
    events += detect(items, sources, rules, NOW)
    ranked = score_events(events, rules, NOW, {i.id: i.points for i in items})
    assert {e.kind for e in ranked} >= {"free-model-added", "model-became-free", "price-drop", "model-listed", "model-no-longer-free", "usage-limit-change"}
    root = make_root(tmp_path / "r")
    cli.save_scan(root, ranked, [], NOW)
    assert cli.cmd_export(argparse.Namespace(date="2026-10-13", studio=None, force=False), root) == 0
    capsys.readouterr()
    code = cli.cmd_verify(argparse.Namespace(studio=str(STUDIO), slug="ai-news-2026-10-13"), root)
    out = capsys.readouterr().out
    assert code == 0 and "0 error(s)" in out, out
