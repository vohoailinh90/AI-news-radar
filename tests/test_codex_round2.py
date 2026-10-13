"""Regression tests for the second Codex review of PR 1 (six findings, named as in that review)."""

import argparse
import json
from datetime import timedelta

import pytest
import yaml

from radar import cli, export
from radar.config import load_rules, load_sources
from radar.detect import detect
from radar.models import Event, Item
from radar.rank import score_events
from radar.sources import openrouter
from radar.store import State
from tests.helpers import NOW, context, make_root, source

RULES = load_rules()
SOURCES = {s.id: s for s in load_sources()}
TAGS = {"vi": "[chưa xác nhận]", "ja": "【未確認】", "en": "[unconfirmed]"}


def item(source_id, title, body="", url="https://www.anthropic.com/news/x"):
    return Item(id=f"{source_id}:{title}", source=source_id, title=title, url=url, published=NOW, body=body)


def ev(kind, title, topic, trust="primary", vendor="anthropic", score=12.0, item_id=None, source_name="Some source", source="s", **kw):
    return Event(kind=kind, vendor=vendor, topic=topic, title=title, url=f"https://e.org/{item_id or kind}", source=source, source_name=source_name,
                 trust=trust, when=NOW, item_id=item_id or kind, score=score, **kw)


# ---- P1 transition ids must survive a replay after a failed state save, yet differ for a genuine repeat
def _model(price):
    return json.dumps({"data": [{"id": "acme/a", "name": "A", "created": 1, "pricing": {"prompt": price, "completion": "0.00001"}}]}).encode()


def test_p1_replaying_a_transition_after_a_failed_state_save_yields_the_same_id(tmp_path):
    src = source("openrouter-models")
    state = State(tmp_path / "s.json")
    openrouter.collect(src, context(tmp_path, {src.url: _model("0.000010")}, now=NOW, state=state))             # baseline
    before_snapshot = json.loads(json.dumps(state.snapshots))                                                    # what disk still holds if the save fails
    first = openrouter.collect(src, context(tmp_path, {src.url: _model("0.000005")}, now=NOW + timedelta(hours=1), state=state))[1]
    state.snapshots = before_snapshot                                                                           # crash: state.json never updated
    replay = openrouter.collect(src, context(tmp_path, {src.url: _model("0.000005")}, now=NOW + timedelta(hours=5), state=state))[1]
    assert [e.item_id for e in first] == [e.item_id for e in replay] and first


def test_p1_a_genuine_repeat_of_the_same_transition_is_still_a_new_event(tmp_path):
    src = source("openrouter-models")
    state, ids = State(tmp_path / "s.json"), []
    for hours, price in enumerate(["0.000010", "0.000005", "0.000010", "0.000005"]):
        ids += [e.item_id for e in openrouter.collect(src, context(tmp_path, {src.url: _model(price)}, now=NOW + timedelta(hours=hours), state=state))[1] if e.kind == "price-drop"]
    assert len(ids) == len(set(ids)) == 2


def test_p1_a_state_file_from_before_this_change_still_loads(tmp_path):
    src = source("openrouter-models")
    state = State(tmp_path / "s.json")
    state.snapshots[src.id] = {"acme/a": {"name": "A", "prompt": 0.00001, "free": False, "created": 1}}           # the old bare mapping
    events = openrouter.collect(src, context(tmp_path, {src.url: _model("0.000005")}, state=state))[1]
    assert [e.kind for e in events] == ["price-drop"]


# ---- P2 vetoes: headline-only when the headline supplies the match, full text when the body does
def test_p2_a_body_that_supplies_the_match_is_also_subject_to_the_vetoes():
    assert [e.kind for e in detect([item("openai-news", "Codex service update", "Learn how to reset usage limits")], SOURCES, RULES, NOW)] == []
    assert [e.kind for e in detect([item("openai-news", "Codex service update", "We reset weekly usage limits for all plans")], SOURCES, RULES, NOW)] == ["quota-reset"]
    # the first-round guarantee still holds: a headline match is not cancelled by words in the summary
    assert [e.kind for e in detect([item("anthropic-news", "Claude usage limits doubled", "Learn how it works under our policy")], SOURCES, RULES, NOW)] == ["usage-limit-change"]


# ---- P1 a page-confirmed source is detected on the confirmed headline only
def test_p1_a_confirmed_source_never_derives_an_event_from_the_mirrors_summary():
    flagged = SOURCES["anthropic-news"]
    assert flagged.opt("confirm_page")
    assert detect([item("anthropic-news", "Claude service update", "Claude is now available for free until Friday")], SOURCES, RULES, NOW) == []
    assert [e.kind for e in detect([item("anthropic-news", "Claude is free for one week", "")], SOURCES, RULES, NOW)] == ["free-promo"]
    plain = item("openai-news", "OpenAI service update", "Codex is free for one week on every plan")                 # an unflagged feed still reads the summary
    assert [e.kind for e in detect([plain], SOURCES, RULES, NOW)] == ["free-promo"]


# ---- P2 release bullet ids follow the content, not the position
def test_p2_editing_a_release_keeps_old_bullet_ids_and_gives_the_new_bullet_a_new_one():
    def release(bullets):
        return Item(id="codex-releases:tag-1", source="codex-releases", title="v1", url="https://github.com/openai/codex/releases/tag/v1",
                    extra={"version": "v1", "bullets": bullets})
    old = {e.title.split(": ", 1)[1]: e.item_id for e in detect([release(["Added background agent mode", "Added a voice mode"])], SOURCES, RULES, NOW)}
    edited = {e.title.split(": ", 1)[1]: e.item_id for e in detect([release(["Added a sandbox profile picker", "Added a voice mode", "Added background agent mode"])], SOURCES, RULES, NOW)}
    assert old["Added background agent mode"] == edited["Added background agent mode"]
    assert old["Added a voice mode"] == edited["Added a voice mode"]
    assert edited["Added a sandbox profile picker"] not in old.values()


# ---- P1 every claim-bearing slide of an unconfirmed story is tagged
@pytest.mark.parametrize("kind,topic,title,figures", [
    ("discount", "discount", "Anthropic cuts pricing by 50% off", {"percent": "50"}),
    ("quota-reset", "quota", "Anthropic doubles limits, up 100%", {"percent": "100"}),
    ("price-drop", "discount", "Acme Alpha input price drops 40% on OpenRouter", {"model": "Acme Alpha", "percent": "40", "old_price": "5.00", "new_price": "3.00"}),
])
def test_p1_the_standalone_stat_slide_of_an_unconfirmed_story_carries_the_tag(kind, topic, title, figures):
    rumour = ev(kind, title, topic, trust="community", figures=figures)
    other = ev("free-promo", "Codex is free for one week", "free", vendor="openai", item_id="o", score=11)
    files = export.build_bundle([rumour, other], RULES, "2026-10-13")
    for lang, tag in TAGS.items():
        spot = yaml.safe_load(files[f"campaigns/ai-news-2026-10-13/posts/{lang}/02-{kind}.yaml"])
        stat = next(s for s in spot["slides"] if s["layout"] == "stat")
        assert tag in stat["eyebrow"] and tag in stat["caption"], (lang, stat)
    verified = ev(kind, title, topic, trust="primary", figures=figures)
    clean = yaml.safe_load(export.build_bundle([verified, other], RULES, "2026-10-13")[f"campaigns/ai-news-2026-10-13/posts/en/02-{kind}.yaml"])
    assert not any(TAGS["en"] in str(s) for s in clean["slides"] if s["layout"] == "stat")


# ---- P2 one story per corroborated pair
def test_p2_an_official_event_and_its_corroborating_report_are_one_story(tmp_path):
    official = ev("quota-reset", "Weekly usage limits have been reset for all Claude plans", "quota", item_id="official", score=12, source="anthropic-news", source_name="Anthropic news")
    report = ev("quota-reset", "Anthropic resets everyone's weekly usage limits", "quota", trust="community", item_id="hn", score=13, source="hn-ai", source_name="Hacker News")
    other = ev("free-promo", "Codex is free for one week", "free", vendor="openai", item_id="free", score=11)
    ranked = [report, official, other]                         # the community report ranks FIRST, as a high-scoring HN story can
    stories, absorbed = export.select_with_absorbed(ranked, RULES)
    assert [s.item_id for s in stories] == ["official", "free"]                      # the primary one is the canonical story even though the report ranked higher
    assert absorbed == {"hn"} and stories[0].also_reported_by == ["Hacker News"]
    assert export.select_with_absorbed([official, report, other], RULES)[0][0] is official          # same result when the official one ranks first
    files = export.build_bundle(ranked, RULES, "2026-10-13")
    brief = yaml.safe_load(files["tools/ai-news-2026-10-13/brief.yaml"])
    assert len(brief["claims"]) == 2 and "Hacker News" in brief["claims"][0]["evidence"] and brief["claims"][0]["verified"] is True
    assert not any(p.endswith("03-quota-reset.yaml") and "/posts/en/" in p for p in files)       # no second spotlight for the same news


def test_p2_the_absorbed_report_is_not_offered_again_as_a_leftover(tmp_path):
    root = make_root(tmp_path)
    official = ev("quota-reset", "Weekly usage limits have been reset for all Claude plans", "quota", item_id="official", score=12, source="anthropic-news")
    report = ev("quota-reset", "Anthropic resets everyone's weekly usage limits", "quota", trust="community", item_id="hn", score=13, source="hn-ai")
    other = ev("free-promo", "Codex is free for one week", "free", vendor="openai", item_id="free", score=11)
    cli.save_scan(root, score_events([report, official, other], RULES, NOW), [], NOW)
    assert cli.cmd_export(argparse.Namespace(date="2026-10-13", studio=None, force=False), root) == 0
    assert cli.load_pending(root) == []


def test_p2_unrelated_stories_are_never_merged():
    a = ev("quota-reset", "Weekly usage limits have been reset for all Claude plans", "quota", item_id="a", source="anthropic-news")
    b = ev("quota-reset", "Codex usage limits reset after outage", "quota", vendor="openai", item_id="b", source="openai-news")
    assert sorted(s.item_id for s in export.select(score_events([a, b], RULES, NOW), RULES)) == ["a", "b"]
