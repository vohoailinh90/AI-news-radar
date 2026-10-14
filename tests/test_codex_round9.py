"""Regression tests for the ninth Codex review of PR 1 (two findings, named as in that review)."""

import random
from datetime import timedelta

from radar import confirm, export
from radar.config import load_rules
from radar.models import Event
from radar.rank import same_story
from tests.helpers import NOW, fixture

RULES = load_rules()
SMALL = {**RULES, "export": {**RULES["export"], "max_per_source_kind": 3, "max_stories": 9}}
WINDOW = timedelta(hours=RULES["scoring"]["corroboration_window_hours"])


def ev(item_id, title, src, trust, score, hours_ago, vendor="anthropic"):
    return Event(kind="quota-reset", vendor=vendor, topic="quota", title=title, url=f"https://e.org/{item_id}", source=src, source_name=src, trust=trust,
                 when=NOW - timedelta(hours=hours_ago), item_id=item_id, score=score)


# ---- P1 a chain of matches cannot stretch a story beyond the corroboration window
def chain_in_time():
    old = ev("old", "Weekly usage limits have been reset for all Claude plans", "anthropic-news", "primary", 20, 80)
    mid = ev("mid", "Anthropic resets everyone's weekly usage limits", "hn-ai", "community", 19, 40)
    new = ev("new", "Claude weekly usage limits reset for everybody again", "reddit", "community", 18, 0)
    in_window = lambda a, b: abs(a.when - b.when) <= WINDOW
    assert same_story(old, mid) and in_window(old, mid) and same_story(mid, new) and in_window(mid, new)      # each link holds...
    assert same_story(old, new) and not in_window(old, new)                                                   # ...but the ends are 80h apart
    return old, mid, new


def test_p1_a_recurrence_is_not_confirmed_by_stale_evidence_through_a_chain():
    old, mid, new = chain_in_time()
    stories, absorbed = export.select_with_absorbed([old, mid, new], SMALL)
    ids = [s.item_id for s in stories]
    assert "old" in ids and "old" not in absorbed                                                              # the old primary story stands on its own
    fresh = next(s for s in stories if s.item_id in ("new", "mid"))
    assert fresh.trust == "community" and not fresh.verified                                                   # the recurrence stays unconfirmed
    assert {"mid", "new"} - {fresh.item_id} <= absorbed and len(stories) == 2


def test_p1_every_group_fits_inside_the_window_and_is_connected():
    for seed in range(200):
        rng = random.Random(seed)
        titles = ["Weekly usage limits have been reset for all Claude plans", "Anthropic resets everyone's weekly usage limits",
                  "Claude weekly usage limits reset for everybody again", "Claude weekly usage reset", "Anthropic limits reset"]
        events = [ev(f"e{n}", rng.choice(titles), rng.choice(["anthropic-news", "hn-ai", "reddit", "x"]), rng.choice(["primary", "community"]),
                     30 - n, rng.choice([0, 20, 40, 60, 80, 120])) for n in range(rng.randint(2, 12))]
        groups = export.story_groups(events, WINDOW)
        assert sorted(e.item_id for g in groups for e in g) == sorted(e.item_id for e in events)               # a partition
        for g in groups:
            assert max(e.when for e in g) - min(e.when for e in g) <= WINDOW, seed                              # nothing stretches past the window
            if len(g) > 1:                                                                                      # ...and the group is connected
                reach, todo = {g[0].item_id}, [g[0]]
                while todo:
                    cur = todo.pop()
                    for o in g:
                        if o.item_id not in reach and same_story(cur, o):
                            reach.add(o.item_id)
                            todo.append(o)
                assert reach == {e.item_id for e in g}, seed


# ---- P1 the page cache keeps what the page says, not one headline's verdict
PAGE = fixture("anthropic_page_head.html")
URL = "https://www.anthropic.com/news/anthropic-cyber-mission"


def mirror(title, trust="primary", item_id="x"):
    return Event(kind="announcement", vendor="anthropic", topic="news", title=title, url=URL, source="anthropic-news", source_name="Anthropic news",
                 trust=trust, when=NOW, item_id=item_id)


def test_p1_two_headlines_for_one_url_are_judged_separately():
    right, wrong = mirror("Introducing the Anthropic Cyber Mission", item_id="r"), mirror("Anthropic gives away Claude for free", item_id="w")
    calls = []
    confirm.confirm_events([right, wrong], {"anthropic-news"}, lambda u, **k: calls.append(u) or PAGE)
    assert right.trust == "primary" and wrong.trust == "community" and len(calls) == 1                         # one fetch, two verdicts
    cache, calls = {}, []
    fetch = lambda u, **k: calls.append(u) or PAGE
    fresh_ok = mirror("Introducing the Anthropic Cyber Mission", item_id="f")
    carried_bad = mirror("Anthropic gives away Claude for free", trust="community", item_id="c")
    confirm.confirm_events([fresh_ok], {"anthropic-news"}, fetch, cache)
    confirm.reconfirm_events([carried_bad], {"anthropic-news": "primary"}, fetch, cache)                         # same URL, shared cache
    assert fresh_ok.trust == "primary" and carried_bad.trust == "community" and len(calls) == 1               # a True for one headline restores nothing else


def test_p1_fresh_evidence_within_the_window_of_the_newest_report_still_confirms_it():
    old = ev("old", "Weekly usage limits have been reset for all Claude plans", "anthropic-news", "primary", 20, 80)
    mid = ev("mid", "Anthropic resets everyone's weekly usage limits", "openai-news", "primary", 19, 40)          # a primary report 40h ago
    new = ev("new", "Claude weekly usage limits reset for everybody again", "reddit", "community", 18, 0)
    stories, absorbed = export.select_with_absorbed([old, mid, new], SMALL)
    ids = [s.item_id for s in stories]
    assert ids == ["old", "mid"] and absorbed == {"new"}                                                       # the cut is anchored on the newest event,
    assert next(s for s in stories if s.item_id == "mid").verified                                            # so the fresh report is backed by mid, not by old
