"""Regression tests for the eighth Codex review of PR 1 (two findings, named as in that review)."""

import itertools
import json
from datetime import timedelta

from radar import cli, confirm, export
from radar.config import load_rules
from radar.models import Event
from radar.rank import same_story
from tests.helpers import NOW, REAL, fixture, make_root

RULES = load_rules()
SMALL = {**RULES, "export": {**RULES["export"], "max_per_source_kind": 2, "max_stories": 9}}


def ev(item_id, title, src, trust, score, vendor="anthropic", when=NOW):
    return Event(kind="quota-reset", vendor=vendor, topic="quota", title=title, url=f"https://e.org/{item_id}", source=src, source_name=src, trust=trust,
                 when=when, item_id=item_id, score=score)


# ---- P2 a chain A~B, B~C with A not matching C is ONE story whatever the order
def chain():
    a = ev("a", "Weekly usage limits have been reset for all Claude plans", "anthropic-news", "primary", 20)
    b = ev("b", "Anthropic resets everyone's weekly usage limits", "hn-ai", "community", 19)
    c = ev("c", "Weekly usage limits reset for Claude plans again", "anthropic-news", "primary", 18)             # same source as a: a and c never match
    assert same_story(a, b) and same_story(b, c) and not same_story(a, c)                                       # the premise of the finding
    return a, b, c


def test_p2_a_transitive_chain_is_one_story_in_every_order():
    for order in itertools.permutations(chain()):
        a, b, c = chain()
        byid = {"a": a, "b": b, "c": c}
        events = [byid[e.item_id] for e in order]
        for rank, e in enumerate(events):
            e.score = 30 - rank                                                                                 # the order is the rank
        stories, absorbed = export.select_with_absorbed(events, SMALL)
        assert len(stories) == 1 and stories[0].trust == "primary", [e.item_id for e in order]
        assert absorbed | {stories[0].item_id} == {"a", "b", "c"}


def test_p2_a_chain_member_is_never_published_ahead_of_a_waiting_canonical():
    a, b, c = chain()
    full = [ev(f"f{n}", t, "anthropic-news", "primary", 40 - n, vendor=v) for n, (t, v) in enumerate(
        [("Codex usage limits reset for all users today", "openai"), ("Gemini usage quota refreshed across all regions", "google")])]
    stories, absorbed = export.select_with_absorbed(full + [a, b, c], SMALL)                                   # the anthropic-news slot is full
    assert [s.item_id for s in stories] == ["f0", "f1"] and {"b"} <= absorbed                                  # b is spent into the waiting canonical, not published


# ---- P2 events whose mirror confirmation failed are re-checked while they wait
def test_p2_a_carried_event_demoted_by_a_failed_confirmation_is_confirmed_on_a_later_scan(tmp_path):
    from tests.helpers import fetcher_for
    root = make_root(tmp_path)
    url = "https://www.anthropic.com/news/anthropic-cyber-mission"
    demoted = Event(kind="announcement", vendor="anthropic", topic="news", title="Introducing the Anthropic Cyber Mission", url=url, source="anthropic-news",
                    source_name="Anthropic news", trust="community", when=NOW, item_id=f"anthropic-news:{url}", score=7.0)
    cli.save_scan(root, [demoted], [], NOW)
    page = fixture("anthropic_page_head.html")
    base = fetcher_for(REAL)
    def fetcher(u, **k):
        return page if u == url else base(u, **k)
    ranked, _ = cli.scan(root, fetcher=fetcher, now=NOW)
    again = next(e for e in ranked if e.item_id == demoted.item_id)
    assert again.trust == "primary" and again.verified                                                         # the page is reachable now: confirmed
    def down(u, **k):
        if u == url:
            raise cli.FetchError("down")
        return base(u, **k)
    still = Event(**{**demoted.__dict__, "trust": "community"})
    cli.save_scan(root, [still], [], NOW)
    ranked, _ = cli.scan(root, fetcher=down, now=NOW)
    assert next(e for e in ranked if e.item_id == demoted.item_id).trust == "community"                         # still unreachable: stays unconfirmed


def test_p2_reconfirmation_only_touches_demoted_events_of_flagged_sources():
    ok = Event(kind="announcement", vendor="anthropic", topic="news", title="Introducing X", url="https://www.anthropic.com/news/x", source="anthropic-news",
               source_name="A", trust="primary", when=NOW, item_id="ok")
    other = Event(kind="new-model", vendor="openai", topic="model", title="Introducing Y", url="https://openai.com/y", source="hn-ai", source_name="H",
                  trust="community", when=NOW, item_id="other")
    calls = []
    confirm.reconfirm_events([ok, other], {"anthropic-news": "primary"}, lambda u, **k: calls.append(u) or b"")
    assert calls == [] and ok.trust == "primary" and other.trust == "community"                                  # already primary / not a mirror source
