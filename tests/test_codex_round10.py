"""Regression tests for the tenth Codex review of PR 1 (two findings, named as in that review)."""

from datetime import timedelta

from radar import cli, confirm, export, rank
from radar.config import load_rules
from radar.http import FetchError
from radar.models import Event, event_from_dict, event_to_dict
from tests.helpers import NOW, REAL, fetcher_for, fixture, make_root

RULES = load_rules()
SMALL = {**RULES, "export": {**RULES["export"], "max_per_source_kind": 3, "max_stories": 9}}
WINDOW = timedelta(hours=RULES["scoring"]["corroboration_window_hours"])


def ev(item_id, title, src, trust, hours_ago, score=20.0):
    return Event(kind="quota-reset", vendor="anthropic", topic="quota", title=title, url=f"https://e.org/{item_id}", source=src, source_name=src, trust=trust,
                 when=NOW - timedelta(hours=hours_ago), item_id=item_id, score=score)


OLD = "Weekly usage limits have been reset for all Claude plans"
MID = "Anthropic resets everyone's weekly usage limits"
NEW = "Claude weekly usage limits reset for everybody again"


# ---- P1 corroboration that crosses a slice boundary is stale evidence for the recurrence
def test_p1_a_peer_cut_off_by_the_slice_does_not_confirm_the_fresh_story():
    old = ev("old", OLD, "anthropic-news", "primary", 60)
    mid = ev("mid", MID, "hn-ai", "community", 40)
    new = ev("new", NEW, "reddit", "community", 0)
    ranked = rank.score_events([old, mid, new], RULES, NOW)
    assert next(e for e in ranked if e.item_id == "mid").corroborated_by == ["anthropic-news"]                 # ranking saw the 20h-old link, as it should
    assert not next(e for e in ranked if e.item_id == "new").corroborated_by                                  # ...and the recurrence has no primary peer of its own
    stories, absorbed = export.select_with_absorbed(ranked, SMALL)
    assert {s.item_id for s in stories} >= {"old"} and len(stories) == 2
    fresh = next(s for s in stories if s.item_id in ("mid", "new"))
    assert not fresh.verified and export.trust_label(fresh, export.TEXT["en"]) == export.TEXT["en"]["community"]      # the old primary is a different story now
    assert next(s for s in stories if s.item_id == "old").verified


def test_p1_a_primary_peer_inside_the_slice_still_confirms_even_when_it_is_below_the_cut():
    quiet = ev("quiet", OLD, "anthropic-news", "primary", 10)
    loud = ev("loud", MID, "hn-ai", "community", 0)
    ranked = rank.score_events([quiet, loud], RULES, NOW)
    next(e for e in ranked if e.item_id == "quiet").score = 0.1                                               # below export.min_score: never a story of its own
    stories, _ = export.select_with_absorbed(ranked, SMALL)
    assert [s.item_id for s in stories] == ["loud"] and stories[0].corroborated_by == ["anthropic-news"] and stories[0].verified


def test_p1_selecting_twice_gives_the_same_corroboration():
    old, mid, new = ev("old", OLD, "anthropic-news", "primary", 60), ev("mid", MID, "hn-ai", "community", 40), ev("new", NEW, "reddit", "community", 0)
    ranked = rank.score_events([old, mid, new], RULES, NOW)
    first = [(s.item_id, s.verified, list(s.corroborated_by)) for s in export.select_with_absorbed(ranked, SMALL)[0]]
    second = [(s.item_id, s.verified, list(s.corroborated_by)) for s in export.select_with_absorbed(ranked, SMALL)[0]]
    assert first == second


# ---- P2 a capped re-confirmation pass rotates, so failing URLs cannot starve the rest of the carried queue
PAGE = fixture("anthropic_page_head.html")
GOOD_URL = "https://www.anthropic.com/news/anthropic-cyber-mission"
GOOD_TITLE = "Introducing the Anthropic Cyber Mission"


def carried(n, url=None, title=None):
    return Event(kind="announcement", vendor="anthropic", topic="news", title=title or f"Headline number {n}", url=url or f"https://www.anthropic.com/news/n{n}",
                 source="anthropic-news", source_name="Anthropic news", trust="community", when=NOW - timedelta(hours=n), item_id=f"c{n}")


def scan_pass(events, when, fetch):
    """One scan's re-confirmation step: a fresh page cache, and the events round-tripped through the pending file."""
    cache = {}
    confirm.reconfirm_events(events, {"anthropic-news": "primary"}, fetch, cache, when)
    return [event_from_dict(event_to_dict(e)) for e in events], cache


def test_p2_failing_urls_do_not_starve_the_ones_behind_them():
    cap = confirm.MAX_CONFIRMATIONS
    queue = [carried(n) for n in range(cap)] + [carried(cap, GOOD_URL, GOOD_TITLE)]          # `cap` dead pages first, one recovered page last
    seen = []

    def fetch(url, **kwargs):
        seen.append(url)
        if url == GOOD_URL:
            return PAGE
        raise FetchError("down")

    queue, cache = scan_pass(queue, NOW, fetch)
    assert len(cache) == cap and GOOD_URL not in seen and queue[-1].trust == "community"                         # scan 1 spends its whole budget on the dead pages
    queue, cache = scan_pass(queue, NOW + timedelta(hours=6), fetch)
    assert GOOD_URL in seen and queue[-1].trust == "primary"                                                   # scan 2 tries the one nobody has tried yet


def test_p2_every_carried_url_is_tried_within_ceil_n_over_cap_scans():
    cap = confirm.MAX_CONFIRMATIONS
    total = cap * 3 + 4
    queue, tried = [carried(n) for n in range(total)], set()

    def fetch(url, **kwargs):
        tried.add(url)
        raise FetchError("down")

    for scan_number in range(-(-total // cap)):
        queue, _ = scan_pass(queue, NOW + timedelta(hours=6 * scan_number), fetch)
    assert tried == {e.url for e in queue}


def test_p2_an_event_the_cap_never_fetched_is_not_marked_as_tried():
    cap = confirm.MAX_CONFIRMATIONS
    queue = [carried(n) for n in range(cap + 3)]
    queue, _ = scan_pass(queue, NOW, lambda url, **kwargs: (_ for _ in ()).throw(FetchError("down")))
    tried = [e for e in queue if e.checked is not None]
    assert len(tried) == cap and all(e.checked is None for e in queue[cap:])                                  # skipped by the cap, so first in line next time


def test_p2_the_attempt_stamp_survives_the_pending_file(tmp_path):
    root = make_root(tmp_path)
    queue = [carried(n) for n in range(confirm.MAX_CONFIRMATIONS + 2)]

    down = fetcher_for({"aider-polyglot": "aider_polyglot_leaderboard.yml"})                                 # one live source (else scan keeps the old files), dead pages

    cli.save_scan(root, queue, [], NOW)
    cli.scan(root, fetcher=down, now=NOW)                                                                      # scan 1
    saved = cli.load_pending(root)
    assert sum(1 for e in saved if e.checked is not None) == confirm.MAX_CONFIRMATIONS
    assert all(e.checked is None or e.checked == NOW for e in saved)
    cli.scan(root, fetcher=down, now=NOW + timedelta(hours=6))                                                 # scan 2 starts with the two nobody tried
    after = {e.item_id: e.checked for e in cli.load_pending(root)}
    assert after[f"c{confirm.MAX_CONFIRMATIONS}"] == NOW + timedelta(hours=6) and after[f"c{confirm.MAX_CONFIRMATIONS + 1}"] == NOW + timedelta(hours=6)


def test_p2_an_old_pending_file_without_the_stamp_still_loads():
    data = event_to_dict(carried(1))
    data.pop("checked", None)
    assert event_from_dict(data).checked is None


def test_p2_a_fresh_event_whose_page_was_fetched_is_stamped_on_the_scan_that_found_it(tmp_path):
    ranked, _ = cli.scan(make_root(tmp_path), fetcher=fetcher_for(REAL), now=NOW)                              # the real mirror feed; its vendor pages are blocked
    mirrored = [e for e in ranked if e.source == "anthropic-news"]
    fetched = [e for e in mirrored if e.checked is not None]
    assert mirrored and all(e.trust == "community" for e in mirrored)
    assert len(fetched) == min(len({e.url for e in mirrored}), confirm.MAX_CONFIRMATIONS) and all(e.checked == NOW for e in fetched)
