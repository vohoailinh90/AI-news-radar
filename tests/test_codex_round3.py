"""Regression tests for the third Codex review of PR 1 (four findings, named as in that review)."""

import json
from datetime import timedelta

import yaml

from radar import export
from radar.config import load_rules
from radar.models import Event
from radar.sources import leaderboard, openrouter
from radar.store import State
from tests.helpers import NOW, context, fixture, source

RULES = load_rules()
TAGS = {"vi": "[chưa xác nhận]", "ja": "【未確認】", "en": "[unconfirmed]"}


def model(model_id, name, created, price="0.000003"):
    return {"id": model_id, "name": name, "created": created, "pricing": {"prompt": price, "completion": "0.00001"}}


def run(tmp_path, state, rows, hours):
    src = source("openrouter-models")
    ctx = context(tmp_path, {src.url: json.dumps({"data": rows}).encode()}, now=NOW + timedelta(hours=hours), state=state)
    return openrouter.collect(src, ctx)[1]


# ---- P2 a model that is re-added is a new occurrence
def test_p2_re_adding_a_model_is_reported_again_for_free_and_paid_models(tmp_path):
    state = State(tmp_path / "s.json")
    base = [model("acme/old", "Old", 1)]
    free = model("acme/x:free", "X", 50, "0")
    paid = model("acme/y", "Y", 60)
    run(tmp_path, state, base, 0)
    ids = []
    for hours, rows in enumerate([base + [free, paid], base, base + [free, paid]], start=1):        # added, removed, re-added
        ids += [(e.kind, e.item_id) for e in run(tmp_path, state, rows, hours) if e.kind in ("free-model-added", "model-listed")]
    added = [i for k, i in ids if k == "free-model-added"]
    listed = [i for k, i in ids if k == "model-listed"]
    assert len(added) == len(set(added)) == 2 and len(listed) == len(set(listed)) == 2


# ---- P1 every string that restates an unconfirmed claim carries the tag
def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def test_p1_no_string_restates_an_unconfirmed_claim_without_the_tag():
    rumour_text = Event(kind="quota-reset", vendor="anthropic", topic="quota", title="Anthropic resets weekly usage limits", url="https://e.org/1",
                        source="hn-ai", source_name="Hacker News", trust="community", when=NOW, item_id="a", score=12)
    rumour_sentence = Event(kind="price-drop", vendor="openai", topic="discount", title="Acme Alpha input price drops 40% on OpenRouter: $5.00 to $3.00 per 1M tokens",
                            url="https://e.org/2", source="openrouter-models", source_name="OpenRouter model list", trust="community", when=NOW, item_id="b", score=11,
                            figures={"model": "Acme Alpha", "percent": "40", "old_price": "5.00", "new_price": "3.00"})
    official = Event(kind="free-promo", vendor="openai", topic="free", title="Codex is free for one week", url="https://e.org/3", source="openai-news",
                     source_name="OpenAI news", trust="primary", when=NOW, item_id="c", score=10)
    files = export.build_bundle([rumour_text, rumour_sentence, official], RULES, "2026-10-13")
    checked = 0
    for lang, tag in TAGS.items():
        for path, text in files.items():
            if f"/posts/{lang}/" not in path:
                continue
            post = yaml.safe_load(text)
            for event in (rumour_text, rumour_sentence):
                claims = {export.headline(event, lang), event.title}
                for s in _strings({"slides": post["slides"], "caption": post["platforms"]["facebook"]["caption"]}):
                    if any(c in s for c in claims):
                        checked += 1
                        assert tag in s, (path, s)
            for s in _strings(post["slides"]):                                                     # the verified story is never tagged
                if "Codex is free" in s or "無料" in s:
                    assert tag not in s
    assert checked > 20                                                                            # the invariant really looked at many strings


# ---- P1 absorption honours the corroboration window
def test_p1_a_stale_primary_event_cannot_absorb_a_fresh_community_report(tmp_path):
    stale = Event(kind="quota-reset", vendor="anthropic", topic="quota", title="Weekly usage limits have been reset for all Claude plans", url="https://e.org/old",
                  source="anthropic-news", source_name="Anthropic news", trust="primary", when=NOW - timedelta(days=5), item_id="old", score=12)
    fresh = Event(kind="quota-reset", vendor="anthropic", topic="quota", title="Anthropic resets everyone's weekly usage limits", url="https://e.org/new",
                  source="hn-ai", source_name="Hacker News", trust="community", when=NOW, item_id="new", score=11)
    stories, absorbed = export.select_with_absorbed([stale, fresh], RULES)
    assert [s.item_id for s in stories] == ["old", "new"] and absorbed == set()                    # a recurrence is its own, unconfirmed story
    assert fresh.verified is False
    within = Event(**{**stale.__dict__, "when": NOW - timedelta(hours=2)})
    stories, absorbed = export.select_with_absorbed([within, fresh], RULES)
    assert [s.item_id for s in stories] == ["old"] and absorbed == {"new"}                         # inside the window they are one story
