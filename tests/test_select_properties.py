"""select_with_absorbed has been the source of a boundary bug in three review rounds in a row. Instead of waiting for the
fourth, check the invariants those bugs broke on a few hundred seeded random event sets."""

import random
from datetime import timedelta

import pytest

from radar import export
from radar.config import load_rules
from radar.models import Event
from radar.rank import same_story
from tests.helpers import NOW

RULES = load_rules()
TITLES = [
    "Weekly usage limits have been reset for all Claude plans", "Claude weekly usage reset", "Anthropic limits reset",
    "Anthropic resets everyone's weekly usage limits", "Codex usage limits reset for all users today", "Codex limits reset after outage",
    "Gemini quota refreshed across every region", "Gemini usage quota refreshed across all regions", "Codex is free for one week",
    "Claude Code usage limits doubled", "Doubled weekly usage limits for Claude Code",
]
VENDORS = {"Claude": "anthropic", "Anthropic": "anthropic", "Codex": "openai", "Gemini": "google"}
SOURCES = ["anthropic-news", "hn-ai", "reddit", "openai-news"]


def vendor_of(title):
    return next(v for k, v in VENDORS.items() if k in title)


def make(rng, n):
    title = rng.choice(TITLES)
    return Event(kind=rng.choice(["quota-reset", "usage-limit-change"]), vendor=vendor_of(title), topic="quota", title=title, url=f"https://e.org/{n}",
                 source=rng.choice(SOURCES), source_name="s", trust=rng.choice(["primary", "community"]),
                 when=NOW - timedelta(hours=rng.choice([0, 1, 30, 100])), item_id=f"e{n}", score=rng.uniform(6, 30))


@pytest.mark.parametrize("seed", range(300))
def test_selection_invariants(seed):
    rng = random.Random(seed)
    rules = {**RULES, "export": {**RULES["export"], "max_per_source_kind": rng.choice([1, 2, 3]), "max_stories": 99}}
    window = timedelta(hours=rules["scoring"]["corroboration_window_hours"])
    events = sorted((make(rng, n) for n in range(rng.randint(2, 14))), key=lambda e: -e.score)
    stories, absorbed = export.select_with_absorbed(events, rules)
    ids = {s.item_id for s in stories}
    left = [e for e in events if e.item_id not in ids and e.item_id not in absorbed]

    def matches(a, b):
        return same_story(a, b) and abs(a.when - b.when) <= window

    assert len(ids) == len(stories)                                                                   # a story appears once
    assert not ids & absorbed                                                                         # nothing is both a story and absorbed
    for i, a in enumerate(stories):                                                                   # (1) no news twice in one issue
        assert not any(matches(a, b) for b in stories[i + 1:]), (seed, a.title)
    for slot in {(s.source, s.kind) for s in stories}:                                                # (2) the slot limit holds
        assert sum(1 for s in stories if (s.source, s.kind) == slot) <= rules["export"]["max_per_source_kind"], seed
    def component(start):                                                                            # reports are folded transitively: a
        seen_, todo = {start.item_id}, [start]                                                       # report may match the story only via
        while todo:                                                                                  # another report of the same news
            cur = todo.pop()
            for other in events:
                if other.item_id not in seen_ and matches(cur, other):
                    seen_.add(other.item_id)
                    todo.append(other)
        return seen_
    represented = {e.item_id for e in stories + left}
    for a in (e for e in events if e.item_id in absorbed):                                            # (3) an absorbed report is represented
        assert component(a) & represented, (seed, a.title)
    for story in (s for s in stories if s.trust != "primary"):                                        # (4) never a report now, its official twin later
        assert not any(matches(story, w) and w.trust == "primary" for w in left), (seed, story.title)
    for waiting in left:                                                                              # (5) nothing waits that a story already covers
        assert not any(matches(waiting, s) for s in stories), (seed, waiting.title)
