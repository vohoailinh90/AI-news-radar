"""Regression tests for the first Codex review of PR 1 (ten findings, named as in that review)."""

import argparse
import json
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from radar import cli, confirm, export
from radar.config import load_rules, load_sources
from radar.detect import detect
from radar.http import FetchError
from radar.models import Event, Item
from radar.sources import SourceError, leaderboard, openrouter, rss
from radar.store import State
from tests.helpers import NOW, REAL, context, fixture, make_root, source

RULES = load_rules()
SOURCES = {s.id: s for s in load_sources()}
FEED = SOURCES["anthropic-news"]


def item(source_id, title, body=""):
    return Item(id=f"{source_id}:{title}", source=source_id, title=title, url="https://www.anthropic.com/news/x", published=NOW, body=body)


# ---- P1 mirror trust: an event stays primary only if the vendor's own page confirms the headline
def test_p1_page_confirmation_uses_the_real_title_tags():
    page = fixture("anthropic_page_head.html").decode()
    assert confirm.confirms(page, "Introducing the Anthropic Cyber Mission")
    assert confirm.confirms(page.replace('<meta property="og:title" content="Introducing the Anthropic Cyber Mission"/>', ""), "Introducing the Anthropic Cyber Mission")
    assert not confirm.confirms(page, "Anthropic gives everyone free Claude forever")
    assert not confirm.confirms(page, "Introducing the Anthropic Cyber")                      # a shorter headline is not this page's headline
    assert confirm.confirms(page, "introducing   THE anthropic cyber mission")                # case and spacing do not matter
    assert confirm.confirms("<!-- <title>decoy</title> -->" + page, "Introducing the Anthropic Cyber Mission")
    assert not confirm.confirms("<!-- <title>Anthropic gives away Claude</title> -->", "Anthropic gives away Claude")   # only a real tag counts


# Synthetic feed: the real Cyber Mission item plus an invented one whose page will not say the same.
TWO_ITEMS = (b'<?xml version="1.0"?><rss version="2.0"><channel>'
             b'<item><title>Introducing the Anthropic Cyber Mission</title><link>https://www.anthropic.com/news/anthropic-cyber-mission</link>'
             b'<guid>1</guid><pubDate>Thu, 08 Oct 2026 00:00:00 +0000</pubDate></item>'
             b'<item><title>Introducing Claude Opus 9</title><link>https://www.anthropic.com/news/opus-9</link>'
             b'<guid>2</guid><pubDate>Thu, 08 Oct 2026 00:00:00 +0000</pubDate></item></channel></rss>')


def _scan_with_pages(tmp_path, page_for):
    def fetcher(url, **k):
        if url == FEED.url:
            return TWO_ITEMS
        if url.startswith("https://www.anthropic.com/news/"):
            return page_for(url)
        raise FetchError(f"{url}: blocked")
    return cli.scan(make_root(tmp_path), fetcher=fetcher, now=NOW)[0]


def test_p1_matching_page_keeps_primary_mismatch_error_and_cap_demote(tmp_path, monkeypatch):
    page = fixture("anthropic_page_head.html")
    events = [e for e in _scan_with_pages(tmp_path / "a", lambda url: page) if e.source == "anthropic-news"]
    mission = next(e for e in events if "Cyber Mission" in e.title)
    assert mission.trust == "primary" and mission.verified                                      # the page says the same thing
    others = [e for e in events if e is not mission]
    assert others and all(e.trust == "community" and not e.verified for e in others)           # their pages (this one) do not
    lying = _scan_with_pages(tmp_path / "b", lambda url: b"<title>Something else entirely \\ Anthropic</title>")
    assert all(e.trust == "community" for e in lying if e.source == "anthropic-news")
    down = _scan_with_pages(tmp_path / "c", lambda url: (_ for _ in ()).throw(FetchError("down")))
    assert all(e.trust == "community" for e in down if e.source == "anthropic-news")
    monkeypatch.setattr(confirm, "MAX_CONFIRMATIONS", 0)
    capped = _scan_with_pages(tmp_path / "d", lambda url: page)
    assert all(e.trust == "community" for e in capped if e.source == "anthropic-news")         # over the cap: never silently trusted


def test_p1_only_flagged_sources_are_checked():
    ev = Event(kind="new-feature", vendor="anthropic", topic="feature", title="x", url="https://github.com/a/b", source="claude-code-changelog",
               source_name="C", trust="primary", when=NOW, item_id="i")
    confirm.confirm_events([ev], {"anthropic-news"}, lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")))
    assert ev.trust == "primary"


# ---- P1 repeated claim must carry the unconfirmed tag
def test_p1_unconfirmed_claim_is_tagged_everywhere_it_is_restated():
    e = Event(kind="quota-reset", vendor="anthropic", topic="quota", title="Anthropic resets weekly usage limits", url="https://e.org", source="hn-ai",
              source_name="Hacker News", trust="community", when=NOW, item_id="i", score=12)
    other = Event(**{**e.__dict__, "kind": "free-promo", "topic": "free", "title": "Codex is free for one week", "trust": "primary", "item_id": "j", "score": 11})
    tags = {"vi": "[chưa xác nhận]", "ja": "【未確認】", "en": "[unconfirmed]"}
    files = export.build_bundle([e, other], RULES, "2026-10-13")
    for lang, tag in tags.items():
        spot = yaml.safe_load(files[f"campaigns/ai-news-2026-10-13/posts/{lang}/02-quota-reset.yaml"])
        lines = [l for l in spot["platforms"]["facebook"]["caption"].splitlines() if "weekly usage limits" in l]
        assert len(lines) >= 2 and all(tag in l for l in lines), (lang, lines)                  # the hook AND the repeated claim
        assert tag in spot["slides"][0]["eyebrow"]
        digest = yaml.safe_load(files[f"campaigns/ai-news-2026-10-13/posts/{lang}/01-digest.yaml"])
        feature = next(s for s in digest["slides"] if s["layout"] == "features")
        assert any(tag in i["text"] and "weekly usage limits" in i["text"] for i in feature["items"])
        assert not any(tag in i["text"] for i in feature["items"] if "free" in i["text"].lower() or "無料" in i["text"])   # verified ones stay clean


# ---- P2 non-finite leaderboard scores
def test_p2_nan_and_inf_scores_are_never_admitted(tmp_path):
    src = source("aider-polyglot")
    raw = b"- {dirname: a, model: M, pass_rate_2: .nan, date: 2026-10-13}\n- {dirname: b, model: N, pass_rate_2: .inf, date: 2026-10-13}\n- {dirname: c, model: O, pass_rate_2: 50.0, date: 2026-10-13}\n"
    assert [e["score"] for e in leaderboard.parse(raw, src)] == [50.0]
    state = State(tmp_path / "s.json")
    leaderboard.collect(src, context(tmp_path, {src.url: raw}, state=state))
    assert state.snapshots[src.id]["best"] == 50.0                                              # no nan/inf persisted


# ---- P2 recurring price transitions
def test_p2_the_same_price_drop_twice_is_two_events(tmp_path):
    src = source("openrouter-models")
    state, ids = State(tmp_path / "s.json"), []
    def model(price):
        return json.dumps({"data": [{"id": "acme/a", "name": "A", "created": 1, "pricing": {"prompt": price, "completion": "0.00001"}}]}).encode()
    for hours, price in enumerate(["0.000010", "0.000005", "0.000010", "0.000005"]):          # 10 -> 5 -> 10 -> 5
        ctx = context(tmp_path, {src.url: model(price)}, now=NOW + timedelta(hours=hours), state=state)
        ids += [e.item_id for e in openrouter.collect(src, ctx)[1] if e.kind == "price-drop"]
    assert len(ids) == 2 and len(set(ids)) == 2


# ---- P1 wrong-shaped state
@pytest.mark.parametrize("payload", ["[]", "null", "false", "0", '"x"', "[1]", '{"seen": []}', '{"snapshots": "x"}', '{"versions": 3}'])
def test_p1_state_that_is_not_the_object_we_write_stops_the_run(tmp_path, payload):
    path = tmp_path / "state.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(RuntimeError, match="unreadable"):
        State.load(path)


def test_p1_an_empty_object_and_a_missing_file_are_fine(tmp_path):
    (tmp_path / "s.json").write_text("{}", encoding="utf-8")
    assert State.load(tmp_path / "s.json").seen == {} and State.load(tmp_path / "nope.json").seen == {}


# ---- P1 the pending file is committed before the seen state
def test_p1_a_crash_between_the_two_writes_loses_nothing(tmp_path, monkeypatch):
    from tests.helpers import fetcher_for
    root = make_root(tmp_path)
    monkeypatch.setattr(State, "save", lambda self, now: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        cli.scan(root, fetcher=fetcher_for(REAL), now=NOW)
    pending = cli.load_pending(root)
    assert pending and not (root / "data" / "state.json").exists()                              # events are on disk, seen marks are not
    monkeypatch.undo()
    again, _ = cli.scan(root, fetcher=fetcher_for(REAL), now=NOW)                               # the retry re-detects them...
    ids = [e.item_id for e in again]
    assert set(ids) >= {e.item_id for e in pending} and len(ids) == len(set(ids))             # ...once, not twice, and not lost
    assert not list((root / "data").glob("*.tmp"))


def test_p1_an_unreadable_pending_file_stops_the_run_instead_of_dropping_events(tmp_path):
    from tests.helpers import fetcher_for
    root = make_root(tmp_path)
    (root / "data").mkdir()
    (root / "data" / "last-scan.json").write_text('{"events": [{"kind": "x"}]}', encoding="utf-8")   # valid JSON, wrong shape
    with pytest.raises(RuntimeError, match="unreadable"):
        cli.scan(root, fetcher=fetcher_for(REAL), now=NOW)


# ---- P2 a post that never declared a status is not replaceable
def test_p2_a_post_without_status_is_refused_even_with_force(tmp_path):
    files = export.build_bundle([Event(kind="free-promo", vendor="openai", topic="free", title="Codex is free for one week", url="https://e.org/1",
                                       source="a", source_name="A", trust="primary", when=NOW, item_id="1", score=11)], RULES, "2026-10-13")
    export.write_bundle(files, tmp_path)
    post = tmp_path / "campaigns/ai-news-2026-10-13/posts/vi/01-free-promo.yaml"
    post.write_text("id: 01-free-promo\nslides: []\n", encoding="utf-8")                        # a mapping, but no status
    assert export.post_status(post) == ("unreadable", False)
    with pytest.raises(export.WouldOverwrite, match="unreadable"):
        export.write_bundle(files, tmp_path, force=True)
    assert post.read_text(encoding="utf-8").startswith("id: 01-free-promo")


# ---- P2 a DTD hidden from a byte-prefix scan
@pytest.mark.parametrize("doc", [
    b'<?xml version="1.0"?><!-- ' + b"x" * 300_000 + b' --><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title></item></channel></rss>',
    b'<?xml version="1.0"?>\n\n   \n<!DOCTYPE rss><rss><channel/></rss>',
    b'<rss><channel><item><title>x</title></item></channel></rss>' + b" " * 300_000,   # padding is harmless on its own
], ids=["entity-after-300KB-comment", "doctype-after-whitespace", "padding-only"])
def test_p2_dtd_is_refused_anywhere_in_the_body_and_padding_alone_is_fine(doc):
    if b"DOCTYPE" in doc:
        with pytest.raises(SourceError, match="DOCTYPE/ENTITY"):
            rss.parse(doc, FEED)
    else:
        assert rss.parse(doc, FEED) == []                                                      # no link, so no item: parsed, not refused


def test_p2_doctype_text_inside_cdata_is_not_a_false_alarm():
    doc = (b"<rss><channel><item><title>t</title><link>https://www.anthropic.com/x</link>"
           b"<description><![CDATA[<!DOCTYPE html><html>an embedded page</html>]]></description></item></channel></rss>")
    assert [i.title for i in rss.parse(doc, FEED)] == ["t"]


# ---- P2 percentage claims need AI context
@pytest.mark.parametrize("title,expected", [
    ("Our team scores 90% employee satisfaction", []),
    ("The study scores 90% of respondents as satisfied", []),
    ("GPT-6 scores 90% on SWE-bench Verified", ["benchmark"]),
    ("Claude Opus 9 scores 88% overall", ["benchmark"]),
    ("Anthropic model scores 61% on the Aider leaderboard", ["benchmark"]),
])
def test_p2_score_percentages_need_a_model_or_a_benchmark(title, expected):
    assert [e.kind for e in detect([item("anthropic-news", title)], SOURCES, RULES, NOW)] == expected


# ---- P2 vetoes read the headline, not the summary
def test_p2_summary_words_cannot_veto_a_valid_headline_but_headline_words_still_do():
    body = "Learn how the new allowance works under our policy."
    assert [e.kind for e in detect([item("anthropic-news", "Claude usage limits doubled", body)], SOURCES, RULES, NOW)] == ["usage-limit-change"]
    assert [e.kind for e in detect([item("anthropic-news", "Why usage limits doubled", body)], SOURCES, RULES, NOW)] == []
    assert [e.kind for e in detect([item("anthropic-news", "Weekly usage limits reset for Claude plans", "no reset policy applies elsewhere")], SOURCES, RULES, NOW)] == ["quota-reset"]
