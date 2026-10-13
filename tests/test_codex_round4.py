"""Regression tests for the fourth Codex review of PR 1 (four findings, named as in that review)."""

import argparse
import json
from datetime import timedelta

import pytest
import yaml

from radar import cli, export
from radar.config import load_rules
from radar.models import Event
from radar.sources import leaderboard, openrouter
from radar.store import State
from tests.helpers import NOW, context, make_root, source

RULES = load_rules()


def model(model_id, name, created, price="0.000003"):
    return {"id": model_id, "name": name, "created": created, "pricing": {"prompt": price, "completion": "0.00001"}}


def collect(tmp_path, state, rows, hours=0):
    src = source("openrouter-models")
    ctx = context(tmp_path, {src.url: json.dumps({"data": rows}).encode()}, now=NOW + timedelta(hours=hours), state=state)
    return openrouter.collect(src, ctx)[1]


# ---- P2 the observed transitions themselves are what is kept, not a baseline that hopes to see them again
def test_p2_every_observed_openrouter_transition_is_emitted_at_once(tmp_path):
    state = State(tmp_path / "s.json")
    base = [model("acme/old", "Old", 1)]
    collect(tmp_path, state, base)
    rows = base + [model(f"acme/m{n}", f"M{n}", 100 + n) for n in range(8)]
    events = collect(tmp_path, state, rows, 1)
    assert [e.figures["model"] for e in events] == [f"M{n}" for n in range(7, -1, -1)]               # all eight, newest first
    assert collect(tmp_path, state, base, 2) == []                                                    # the models vanishing later changes nothing...
    assert state.snapshots["openrouter-models"]["models"].keys() == {"acme/old"}                      # ...and nothing was held back in the snapshot


def test_p2_a_burst_is_spread_over_issues_by_the_exporter_without_losing_any_of_it(tmp_path):
    root = make_root(tmp_path)
    state = State(tmp_path / "s.json")
    base = [model("acme/old", "Old", 1)]
    collect(tmp_path, state, base)
    events = collect(tmp_path, state, base + [model(f"acme/m{n}", f"M{n}", 100 + n) for n in range(8)], 1)
    from radar.rank import score_events
    cli.save_scan(root, score_events(events, RULES, NOW), [], NOW)                                     # persisted the moment they were observed
    assert cli.cmd_export(argparse.Namespace(date="2026-10-13", studio=None, force=False), root) == 0
    brief = yaml.safe_load((root / "out/ai-news-2026-10-13/tools/ai-news-2026-10-13/brief.yaml").read_text(encoding="utf-8"))
    exported = {c["text"].split(" was ")[0] for c in brief["claims"]}
    waiting = {e.figures["model"] for e in cli.load_pending(root)}
    assert exported | waiting == {f"M{n}" for n in range(8)} and not exported & waiting             # each exactly once: issue now or pending


def test_p2_the_exporter_limits_one_source_and_kind_so_a_burst_cannot_fill_the_issue():
    burst = [Event(kind="model-listed", vendor="other", topic="model", title=f"M{n} was just added to OpenRouter's model list", url=f"https://e.org/{n}",
                   source="openrouter-models", source_name="OpenRouter model list", trust="primary", when=NOW, item_id=f"m{n}", score=20 - n,
                   figures={"model": f"M{n}"}) for n in range(6)]
    other = Event(kind="free-promo", vendor="openai", topic="free", title="Codex is free for one week", url="https://e.org/f", source="openai-news",
                  source_name="OpenAI news", trust="primary", when=NOW, item_id="free", score=11)
    stories, absorbed = export.select_with_absorbed(burst + [other], RULES)
    limit = RULES["export"]["max_per_source_kind"]
    assert [s.item_id for s in stories] == ["m0", "m1", "free"][: limit + 1] and absorbed == set()    # the rest are neither stories nor absorbed: they wait


def test_p2_every_new_leaderboard_entry_is_emitted_at_once(tmp_path):
    src = source("aider-polyglot")
    state = State(tmp_path / "s.json")
    def rows(n):
        return ("".join(f"- {{dirname: e{i}, model: M{i}, pass_rate_2: {10 + i}.0, date: 2026-10-13}}\n" for i in range(n))).encode()
    leaderboard.collect(src, context(tmp_path, {src.url: rows(1)}, state=state))
    events = leaderboard.collect(src, context(tmp_path, {src.url: rows(9)}, state=state))[1]
    assert sorted(e.figures["model"] for e in events) == sorted(f"M{i}" for i in range(1, 9))
    assert set(state.snapshots[src.id]["ids"]) == {f"e{i}" for i in range(9)}                         # everything seen was reported, so everything is recorded


# ---- P2 state moves only after the source has been processed successfully
def test_p2_a_failure_midway_leaves_the_openrouter_snapshot_untouched(tmp_path, monkeypatch):
    state = State(tmp_path / "s.json")
    collect(tmp_path, state, [model("acme/old", "Old", 1)])
    before = json.loads(json.dumps(state.snapshots))
    monkeypatch.setattr(openrouter, "vendor_of_prefix", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        collect(tmp_path, state, [model("acme/old", "Old", 1), model("acme/new", "New", 2)], 1)
    assert state.snapshots == before


def test_p2_scan_rolls_back_whatever_a_failing_source_changed_in_the_shared_state(tmp_path, monkeypatch):
    from tests.helpers import fetcher_for, REAL
    root = make_root(tmp_path)
    def half_done(src, ctx):
        ctx.state.snapshots[src.id] = {"poisoned": True}
        ctx.state.versions[src.id] = "99.9.9"
        raise KeyError("died after touching the state")
    monkeypatch.setitem(cli.COLLECTORS, "rss", type("M", (), {"collect": staticmethod(half_done)}))
    cli.scan(root, fetcher=fetcher_for(REAL), now=NOW)                                                 # changelog + leaderboard still succeed, so state is saved
    saved = json.loads((root / "data" / "state.json").read_text(encoding="utf-8"))
    assert "anthropic-news" not in saved["snapshots"] and "anthropic-news" not in saved["versions"]


def test_p2_an_absurd_created_timestamp_is_tolerated_not_fatal(tmp_path):
    state = State(tmp_path / "s.json")
    collect(tmp_path, state, [model("acme/old", "Old", 1)])
    events = collect(tmp_path, state, [model("acme/old", "Old", 1), model("acme/new", "New", 10 ** 18), model("acme/odd", "Odd", "soon")], 1)
    assert sorted(e.figures["model"] for e in events) == ["New", "Odd"] and all(e.when == NOW + timedelta(hours=1) for e in events)
    assert set(state.snapshots["openrouter-models"]["models"]) == {"acme/old", "acme/new", "acme/odd"}


# ---- P1 the brief marks unconfirmed rows too
def test_p1_the_brief_never_states_an_unconfirmed_report_as_fact():
    rumour = Event(kind="quota-reset", vendor="anthropic", topic="quota", title="Anthropic resets weekly usage limits", url="https://e.org/1", source="hn-ai",
                   source_name="Hacker News", trust="community", when=NOW, item_id="a", score=12)
    official = Event(kind="free-promo", vendor="openai", topic="free", title="Codex is free for one week", url="https://e.org/2", source="openai-news",
                     source_name="OpenAI news", trust="primary", when=NOW, item_id="b", score=11)
    brief = yaml.safe_load(export.build_bundle([rumour, official], RULES, "2026-10-13")["tools/ai-news-2026-10-13/brief.yaml"])
    bad = next(f for f in brief["features"] if "weekly usage limits" in f["fact"])
    claim = next(c for c in brief["claims"] if "weekly usage limits" in c["text"])
    assert "[unconfirmed]" in bad["fact"] and "[unconfirmed]" in claim["text"] and claim["verified"] is False
    good = next(f for f in brief["features"] if "free for one week" in f["fact"])
    good_claim = next(c for c in brief["claims"] if "free for one week" in c["text"])
    assert "[unconfirmed]" not in good["fact"] and "[unconfirmed]" not in good_claim["text"] and good_claim["verified"] is True
