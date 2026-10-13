"""Regression tests for the sixth Codex review of PR 1 (four findings, named as in that review)."""

import json
from datetime import timedelta

from radar import cli, export
from radar.config import load_rules
from radar.models import Event
from radar.sources import leaderboard, openrouter
from radar.store import State
from tests.helpers import NOW, context, source

RULES = load_rules()


def ev(item_id, title, src, trust, vendor, score, kind="quota-reset", topic="quota", when=NOW):
    return Event(kind=kind, vendor=vendor, topic=topic, title=title, url=f"https://e.org/{item_id}", source=src, source_name=src, trust=trust,
                 when=when, item_id=item_id, score=score)


# ---- P2 an expired occurrence cannot be refreshed by its replay
def test_p2_an_expired_pending_occurrence_still_suppresses_its_replay():
    first = ev("x", "Weekly usage limits have been reset for all Claude plans", "anthropic-news", "primary", "anthropic", 12, when=NOW - timedelta(days=30))
    replay = ev("x", "Weekly usage limits have been reset for all Claude plans", "anthropic-news", "primary", "anthropic", 12, when=NOW)
    other = ev("y", "Codex usage limits reset for all users", "openai-news", "primary", "openai", 11)
    fresh, carried = cli.merge_pending([replay, other], [first], NOW - timedelta(days=7))
    assert [f.item_id for f in fresh] == ["y"] and carried == []                                     # expired, so not carried; and not resurrected either
    fresh, carried = cli.merge_pending([replay], [], NOW - timedelta(days=7))
    assert [f.item_id for f in fresh] == ["x"]                                                       # no pending copy at all: a normal fresh event


# ---- P2 the destination slot of a replacement is enforced too
def test_p2_a_primary_replacement_cannot_push_its_slot_past_the_limit():
    rules = {**RULES, "export": {**RULES["export"], "max_per_source_kind": 2, "max_stories": 9}}
    p1 = ev("p1", "Weekly usage limits reset for Claude plans", "anthropic-news", "primary", "anthropic", 20)
    p2 = ev("p2", "Codex usage limits reset for all users today", "anthropic-news", "primary", "openai", 19)
    c3 = ev("c3", "Gemini quota refreshed across every region", "hn-ai", "community", "google", 18)
    p3 = ev("p3", "Gemini usage quota refreshed across all regions", "anthropic-news", "primary", "google", 17)
    stories, absorbed = export.select_with_absorbed([p1, p2, c3, p3], rules)
    ids = [s.item_id for s in stories]
    assert ids.count("p3") == 1 and "c3" not in ids and absorbed == {"c3"}                           # the official version is the story
    assert sum(1 for s in stories if s.source == "anthropic-news") <= 2                              # ...and the slot limit still holds
    assert "p2" not in ids and "p2" not in absorbed                                                  # the displaced one waits for a later issue


# ---- P2 a new listing is aged from when it was observed, not from when the model was created
def test_p2_a_newly_observed_listing_keeps_its_observation_time_and_the_creation_date_separately(tmp_path):
    src = source("openrouter-models")
    state = State(tmp_path / "s.json")
    def feed(*rows):
        return context(tmp_path, {src.url: json.dumps({"data": list(rows)}).encode()}, now=NOW, state=state)
    old_row = {"id": "acme/old", "name": "Old", "created": 1, "pricing": {"prompt": "0.000003", "completion": "0.00001"}}
    ancient = {"id": "acme/ancient", "name": "Ancient", "created": int((NOW - timedelta(days=90)).timestamp()), "pricing": {"prompt": "0.000003", "completion": "0.00001"}}
    ancient_free = {"id": "acme/ancient-f:free", "name": "AncientFree", "created": int((NOW - timedelta(days=90)).timestamp()), "pricing": {"prompt": "0", "completion": "0"}}
    openrouter.collect(src, feed(old_row))
    events = openrouter.collect(src, feed(old_row, ancient, ancient_free))[1]
    assert {e.figures["model"] for e in events} == {"Ancient", "AncientFree"}
    assert all(e.when == NOW for e in events)                                                        # survives the 7-day carry horizon
    assert all("created" in e.detail for e in events)                                                # the date is kept, as text


# ---- P2 occurrence stamps are monotonic, not wall-clock seconds
def test_p2_add_remove_re_add_inside_one_second_still_gives_distinct_ids_for_both_collectors(tmp_path):
    src = source("aider-polyglot")
    state = State(tmp_path / "s.json")
    def rows(*ids):
        return ("".join(f"- {{dirname: {i}, model: M{i}, pass_rate_2: 50.0, date: 2026-10-13}}\n" for i in ids)).encode()
    def run(raw):
        return leaderboard.collect(src, context(tmp_path, {src.url: raw}, now=NOW, state=state))[1]            # the SAME instant every time
    run(rows("a"))
    ids = [e.item_id for raw in (rows("a", "b"), rows("a"), rows("a", "b")) for e in run(raw)]
    assert len(ids) == len(set(ids)) == 2

    orr, state2 = source("openrouter-models"), State(tmp_path / "s2.json")
    def models(*names):
        return json.dumps({"data": [{"id": f"acme/{n}", "name": n, "created": 5, "pricing": {"prompt": "0.000003", "completion": "0.00001"}} for n in names]}).encode()
    def run2(raw):
        return openrouter.collect(orr, context(tmp_path, {orr.url: raw}, now=NOW, state=state2))[1]
    run2(models("a"))
    ids2 = [e.item_id for raw in (models("a", "b"), models("a"), models("a", "b")) for e in run2(raw) if e.kind == "model-listed"]
    assert len(ids2) == len(set(ids2)) == 2
