"""Regression tests for the fifth Codex review of PR 1 (three findings, named as in that review)."""

from datetime import timedelta

import yaml

from radar import cli, export
from radar.config import load_rules
from radar.models import Event
from radar.sources import leaderboard
from radar.store import State
from tests.helpers import NOW, context, source

RULES = load_rules()
TAGS = {"vi": "[chưa xác nhận]", "ja": "【未確認】", "en": "[unconfirmed]"}


def rows(*entries):
    return ("".join(f"- {{dirname: {d}, model: {m}, pass_rate_2: {s}, date: 2026-10-13}}\n" for d, m, s in entries)).encode()


def run(tmp_path, state, raw, hours):
    src = source("aider-polyglot")
    return leaderboard.collect(src, context(tmp_path, {src.url: raw}, now=NOW + timedelta(hours=hours), state=state))[1]


# ---- P2 leaderboard observations are occurrences: replay-stable, and a re-added row is a new one
def test_p2_a_replay_after_a_failed_state_save_repeats_the_id_even_if_the_score_moved(tmp_path):
    state = State(tmp_path / "s.json")
    run(tmp_path, state, rows(("a", "A", 40.0)), 0)                                                  # baseline
    saved = {k: dict(v) for k, v in state.snapshots.items()}
    first = run(tmp_path, state, rows(("a", "A", 40.0), ("b", "B", 50.0)), 1)
    state.snapshots = saved                                                                         # crash: state.json was not written
    replay = run(tmp_path, state, rows(("a", "A", 40.0), ("b", "B", 52.0)), 5)                      # ...and B's score moved before the retry
    assert [e.item_id for e in first] == [e.item_id for e in replay] and first[0].figures["score"] == "50" and replay[0].figures["score"] == "52"


def test_p2_the_copy_observed_first_wins_when_a_replay_meets_its_pending_event(tmp_path):
    state = State(tmp_path / "s.json")
    run(tmp_path, state, rows(("a", "A", 40.0)), 0)
    saved = {k: dict(v) for k, v in state.snapshots.items()}
    original = run(tmp_path, state, rows(("a", "A", 40.0), ("b", "B", 50.0)), 1)[0]
    state.snapshots = saved
    replay = run(tmp_path, state, rows(("a", "A", 40.0), ("b", "B", 52.0)), 5)[0]
    fresh, carried = cli.merge_pending([replay], [original], NOW - timedelta(days=7))
    assert fresh == [] and [c.figures["score"] for c in carried] == ["50"]                          # the transition as first observed survives
    old = Event(**{**original.__dict__, "when": NOW - timedelta(days=30)})
    fresh, carried = cli.merge_pending([replay], [old], NOW - timedelta(days=7))                    # an expired pending copy must not eat the fresh one
    assert [f.figures["score"] for f in fresh] == ["52"] and carried == []


def test_p2_a_removed_and_re_added_row_is_a_new_occurrence(tmp_path):
    state = State(tmp_path / "s.json")
    run(tmp_path, state, rows(("a", "A", 40.0)), 0)
    ids = []
    for hours, raw in enumerate([rows(("a", "A", 40.0), ("b", "B", 50.0)), rows(("a", "A", 40.0)), rows(("a", "A", 40.0), ("b", "B", 50.0))], start=1):
        ids += [e.item_id for e in run(tmp_path, state, raw, hours)]
    assert len(ids) == len(set(ids)) == 2


# ---- P2 the per-source-and-kind slots follow the canonical event
def _ev(item_id, title, source, trust, vendor, score):
    return Event(kind="quota-reset", vendor=vendor, topic="quota", title=title, url=f"https://e.org/{item_id}", source=source, source_name=source,
                 trust=trust, when=NOW, item_id=item_id, score=score)


def test_p2_replacing_a_community_story_by_its_primary_twin_moves_the_slot_charge():
    rules = {**RULES, "export": {**RULES["export"], "max_per_source_kind": 2, "max_stories": 9}}
    c_a = _ev("c_a", "Anthropic resets everyone's weekly usage limits", "hn-ai", "community", "anthropic", 20)
    c_b = _ev("c_b", "Codex usage limits reset for all users today", "hn-ai", "community", "openai", 19)
    p_a = _ev("p_a", "Weekly usage limits have been reset for all Claude plans", "anthropic-news", "primary", "anthropic", 18)
    c_c = _ev("c_c", "Gemini quota refreshed across every region", "hn-ai", "community", "google", 17)
    stories, absorbed = export.select_with_absorbed([c_a, c_b, p_a, c_c], rules)
    assert [s.item_id for s in stories] == ["p_a", "c_b", "c_c"] and absorbed == {"c_a"}              # hn-ai holds 2 stories, not 3, so c_c still fits
    # and the primary source's own slot is charged: a third anthropic-news story is deferred
    extra = [_ev(f"x{n}", t, "anthropic-news", "primary", v, 10 - n) for n, (t, v) in enumerate(
        [("Weekly usage limits reset for Codex plans", "openai"), ("Weekly usage limits reset for Gemini plans", "google")])]
    stories, _ = export.select_with_absorbed([c_a, p_a] + extra, rules)
    assert [s.source for s in stories].count("anthropic-news") == 2


# ---- P1 poll options (and the digest item headings) restate the claim too
def test_p1_poll_options_and_digest_item_titles_of_an_unconfirmed_story_carry_the_tag():
    rumour = _ev("r", "Anthropic resets weekly usage limits", "hn-ai", "community", "anthropic", 12)
    official = Event(kind="free-promo", vendor="openai", topic="free", title="Codex is free for one week", url="https://e.org/o", source="openai-news",
                     source_name="OpenAI news", trust="primary", when=NOW, item_id="o", score=11)
    files = export.build_bundle([rumour, official], RULES, "2026-10-13")
    for lang, tag in TAGS.items():
        digest = yaml.safe_load(files[f"campaigns/ai-news-2026-10-13/posts/{lang}/01-digest.yaml"])
        poll = next(s for s in digest["slides"] if s["layout"] == "poll")
        label = export.TEXT[lang]["labels"]["quota-reset"]
        flagged = [o for o in poll["options"] if label in o]
        assert flagged and all(tag in o for o in flagged), (lang, poll["options"])
        clean = [o for o in poll["options"] if export.TEXT[lang]["labels"]["free-promo"] in o]
        assert clean and not any(tag in o for o in clean)
        feature = next(s for s in digest["slides"] if s["layout"] == "features")
        titles = [i["title"] for i in feature["items"]]
        assert all(tag in t for t in titles if label in t) and not any(tag in t for t in titles if export.TEXT[lang]["labels"]["free-promo"] in t)
