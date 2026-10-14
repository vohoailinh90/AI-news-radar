"""Regression tests for the seventh Codex review of PR 1 (four findings, named as in that review)."""

import json
from datetime import timedelta

from radar import export
from radar.config import load_rules
from radar.models import Event
from radar.sources import occurrence, openrouter
from radar.store import State
from tests.helpers import NOW, context, source

RULES = load_rules()
SMALL = {**RULES, "export": {**RULES["export"], "max_per_source_kind": 2, "max_stories": 9}}


def ev(item_id, title, src, trust, vendor, score, kind="quota-reset", topic="quota"):
    return Event(kind=kind, vendor=vendor, topic=topic, title=title, url=f"https://e.org/{item_id}", source=src, source_name=src, trust=trust,
                 when=NOW, item_id=item_id, score=score)


# A: Anthropic news (primary) and two community reports of the same news; B: Gemini news, used as filler
A_OFFICIAL = "Weekly usage limits have been reset for all Claude plans"
A_HN = "Anthropic resets everyone's weekly usage limits"
A_REDDIT = "Claude weekly usage limits reset for everybody"


# ---- P2 three or more reports of one story collapse into one
def test_p2_every_picked_report_matched_by_the_canonical_event_is_absorbed():
    # two community reports that do NOT match each other (they share one word), but each matches the official event
    hn = ev("hn", "Claude weekly usage reset", "hn-ai", "community", "anthropic", 20)
    rd = ev("rd", "Anthropic limits reset", "reddit", "community", "anthropic", 19)
    official = ev("official", A_OFFICIAL, "anthropic-news", "primary", "anthropic", 18)
    from radar.rank import same_story
    assert not same_story(hn, rd) and same_story(hn, official) and same_story(rd, official)         # the premise of the finding
    stories, absorbed = export.select_with_absorbed([hn, rd, official], SMALL)
    assert [s.item_id for s in stories] == ["official"] and absorbed == {"hn", "rd"}
    assert sorted(stories[0].also_reported_by) == ["hn-ai", "reddit"]


# ---- P2 a displaced or skipped story keeps absorbing its later reports
def test_p2_a_report_of_a_story_is_folded_into_it_however_the_slots_fall():
    p1 = ev("p1", "Weekly usage limits reset for Claude plans", "anthropic-news", "primary", "anthropic", 20)
    p2 = ev("p2", "Codex usage limits reset for all users today", "anthropic-news", "primary", "openai", 19)
    c3 = ev("c3", "Gemini quota refreshed across every region", "hn-ai", "community", "google", 18)
    p3 = ev("p3", "Gemini usage quota refreshed across all regions", "anthropic-news", "primary", "google", 17)     # displaces p2
    late = ev("late", "Codex usage limits have been reset for every user", "reddit", "community", "openai", 16)       # a report of p2
    stories, absorbed = export.select_with_absorbed([p1, p2, c3, p3, late], SMALL)
    ids = [s.item_id for s in stories]
    assert ids == ["p1", "p2"] and absorbed == {"c3", "late"}                                 # each report is folded into its story; none leaks out
    assert "p3" not in absorbed                                                              # p3, the official Gemini story, waits with c3 attached


def test_p2_a_report_of_a_story_skipped_for_a_full_slot_is_folded_into_it():
    first = [ev(f"f{n}", t, "anthropic-news", "primary", v, 20 - n) for n, (t, v) in enumerate(
        [("Weekly usage limits reset for Claude plans", "anthropic"), ("Codex usage limits reset for all users today", "openai")])]
    skipped = ev("skipped", "Gemini usage quota refreshed across all regions", "anthropic-news", "primary", "google", 17)    # slot full: waits
    report = ev("report", "Gemini quota refreshed across every region", "hn-ai", "community", "google", 16)
    stories, absorbed = export.select_with_absorbed(first + [skipped, report], SMALL)
    ids = [s.item_id for s in stories]
    assert ids == ["f0", "f1"] and absorbed == {"report"}                                    # the report does not get published ahead of its official twin


def test_p2_a_primary_event_arriving_after_its_skipped_community_twin_becomes_the_story():
    full = [ev(f"h{n}", t, "hn-ai", "community", v, 20 - n) for n, (t, v) in enumerate(
        [("Weekly usage limits reset for Claude plans", "anthropic"), ("Codex usage limits reset for all users today", "openai")])]
    skipped = ev("skipped", "Gemini quota refreshed across every region", "hn-ai", "community", "google", 17)           # hn-ai slot is full
    official = ev("official", "Gemini usage quota refreshed across all regions", "google-news", "primary", "google", 16)
    stories, absorbed = export.select_with_absorbed(full + [skipped, official], SMALL)
    assert [s.item_id for s in stories] == ["h0", "h1", "official"] and absorbed == {"skipped"}


# ---- P2 a price-drop id names the generation, not the observed prices
def test_p2_a_retry_that_sees_a_different_price_repeats_the_original_id(tmp_path):
    src = source("openrouter-models")
    state = State(tmp_path / "s.json")
    def feed(price, hours):
        raw = json.dumps({"data": [{"id": "acme/a", "name": "A", "created": 1, "pricing": {"prompt": price, "completion": "0.00001"}}]}).encode()
        return openrouter.collect(src, context(tmp_path, {src.url: raw}, now=NOW + timedelta(hours=hours), state=state))[1]
    feed("0.000010", 0)
    saved = json.loads(json.dumps(state.snapshots))
    first = feed("0.000005", 1)
    state.snapshots = saved                                                                         # crash before the state was saved
    retry = feed("0.000004", 5)                                                                     # ...and the price moved again
    assert [e.item_id for e in first] == [e.item_id for e in retry] and first and first[0].figures["new_price"] != retry[0].figures["new_price"]


# ---- P2 snapshots written by the previous implementation keep their stamp
def test_p2_a_snapshot_with_only_a_taken_stamp_keeps_that_stamp_as_its_occurrence():
    assert occurrence({"taken": "20261013T220000", "models": {}}) == "20261013T220000"
    assert occurrence({"gen": 4, "taken": "20261013T220000"}) == "g4"
    assert occurrence({"models": {}}) == "legacy" and occurrence({"acme/x": {}}) == "legacy"
