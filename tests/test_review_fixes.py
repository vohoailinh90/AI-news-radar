"""Regression tests for the independent review's findings (numbered as in the review)."""

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
from radar.sources import SourceError, rss
from radar.textsafe import has_link
from tests.helpers import NOW, fixture, make_root, source

RULES = load_rules()
SOURCES = {s.id: s for s in load_sources()}


def detected(source_id, title, body="", points=0):
    item = Item(id=f"{source_id}:{title}", source=source_id, title=title, url="https://example.org/x", published=NOW, body=body, points=points)
    return score_events(detect([item], SOURCES, RULES, NOW), RULES, NOW)


def test_1_a_rule_detected_new_model_is_posted_with_its_own_headline_in_every_language():
    files = export.build_bundle(detected("openai-news", "Introducing GPT-6") + detected("anthropic-news", "Introducing Claude Opus 9"), RULES, "2026-10-13")
    for lang in ("vi", "ja", "en"):
        digest = yaml.safe_load(files[f"campaigns/ai-news-2026-10-13/posts/{lang}/01-digest.yaml"])
        text = yaml.dump(digest, allow_unicode=True)
        assert "Introducing GPT-6" in text and "Introducing Claude Opus 9" in text
        assert "danh sách" not in text and "一覧" not in text and "model list" not in text      # not the OpenRouter sentence
    brief = yaml.safe_load(files["tools/ai-news-2026-10-13/brief.yaml"])
    assert all(c["text"].strip().startswith("Introducing") for c in brief["claims"])


def test_2_event_with_a_dotted_model_name_cannot_crash_the_export():
    event = Event(kind="free-model-added", vendor="other", topic="free", title="Z.AI: GLM 4.6 is available free of charge on OpenRouter",
                  url="https://openrouter.ai/z-ai/glm", source="openrouter-models", source_name="OpenRouter model list", trust="primary",
                  when=NOW, figures={"model": "Z.AI: GLM 4.6"}, item_id="x", score=10)
    assert not has_link(event.title) and not has_link(event.figures["model"])
    files = export.build_bundle([event, Event(**{**event.__dict__, "item_id": "y", "kind": "model-became-free"})], RULES, "2026-10-13")
    assert all(not has_link(yaml.dump(yaml.safe_load(t), allow_unicode=True)) for p, t in files.items() if "/posts/" in p)


def test_2_a_link_that_still_gets_through_is_exit_5_not_a_crash(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    cli.save_scan(root, [Event(kind="x", vendor="openai", topic="model", title="t", url="https://e.org", source="s", source_name="S",
                               trust="primary", when=NOW, item_id="i", score=10)], [], NOW)
    monkeypatch.setattr(export, "build_bundle", lambda *a, **k: (_ for _ in ()).throw(ValueError("a post contains link-looking text")))
    assert cli.cmd_export(argparse.Namespace(date="2026-10-13", studio=None, force=False), root) == 5


def test_3_unconfirmed_story_is_tagged_on_the_caption_line_that_states_it():
    stories = detected("hn-ai", "Anthropic resets everyone's weekly usage limits") + detected("openai-news", "Introducing GPT-6")
    assert [s.verified for s in stories if s.source == "hn-ai"] == [False]
    files = export.build_bundle(sorted(stories, key=lambda e: -e.score), RULES, "2026-10-13")
    tags = {"vi": "[chưa xác nhận]", "ja": "【未確認】", "en": "[unconfirmed]"}
    for lang, tag in tags.items():
        caption = yaml.safe_load(files[f"campaigns/ai-news-2026-10-13/posts/{lang}/01-digest.yaml"])["platforms"]["facebook"]["caption"]
        rumour_lines = [line for line in caption.splitlines() if "weekly usage limits" in line]
        assert rumour_lines and all(tag in line for line in rumour_lines), (lang, caption)
        assert not any(tag in line for line in caption.splitlines() if "GPT-6" in line)


def test_3_every_story_names_its_source_in_the_digest():
    stories = detected("openai-news", "Introducing GPT-6") + detected("anthropic-news", "Introducing Claude Opus 9")
    files = export.build_bundle(sorted(stories, key=lambda e: -e.score), RULES, "2026-10-13")
    post = yaml.safe_load(files["campaigns/ai-news-2026-10-13/posts/en/01-digest.yaml"])["platforms"]["facebook"]
    assert "OpenAI news" in post["caption"] and "Anthropic news" in post["caption"]
    assert "OpenAI news" in post["first_comment"] and "Anthropic news" in post["first_comment"] and "{link}" in post["first_comment"]


def test_6_release_notes_follow_the_changelog_rules_bullet_by_bullet():
    codex = SOURCES["codex-releases"]
    items = rss.parse(fixture("synthetic_releases.atom"), codex)
    assert items[0].extra["bullets"] == ["Doubled the weekly usage limits for all plans"] and items[0].extra["version"] == "v1.2.0"
    events = detect(items, SOURCES, RULES, NOW)
    assert [e.kind for e in events] == ["usage-limit-change"] and events[0].title.startswith("Codex releases v1.2.0: Doubled")
    fix_only = Item(id="c:1", source="codex-releases", title="0.46.0", url="https://github.com/openai/codex/releases/tag/0.46.0",
                    body="Fix rate limit reset display", extra={"version": "0.46.0", "bullets": ["Fix rate limit reset display"]})
    assert detect([fix_only], SOURCES, RULES, NOW) == []                                    # a fix note is not a reset
    ux = Item(id="c:2", source="codex-releases", title="0.47.0", url="https://github.com/openai/codex/releases/tag/0.47.0",
              extra={"version": "0.47.0", "bullets": ["Added a banner that shows when your weekly usage limit reset happens"]})
    assert [e.kind for e in detect([ux], SOURCES, RULES, NOW)] == []                        # quota-reset never fires from release notes


def test_10_a_percent_in_the_body_never_becomes_a_figure():
    events = detected("anthropic-news", "Weekly usage limits have been reset for all Claude plans", body="Usage was up 3% last week.")
    assert events[0].kind == "quota-reset" and events[0].figures == {}
    files = export.build_bundle(events + detected("openai-news", "Introducing GPT-6"), RULES, "2026-10-13")
    spot = yaml.safe_load(files["campaigns/ai-news-2026-10-13/posts/vi/02-quota-reset.yaml"]) if "campaigns/ai-news-2026-10-13/posts/vi/02-quota-reset.yaml" in files else None
    assert all(s["layout"] != "stat" for p, t in files.items() if "/posts/" in p for s in yaml.safe_load(t)["slides"])


def test_9_force_removes_stale_drafts_of_the_issue_but_never_a_non_draft(tmp_path):
    first = [Event(kind="free-promo", vendor="openai", topic="free", title="Codex is free for one week", url="https://e.org/1", source="a",
                   source_name="A", trust="primary", when=NOW, item_id="1", score=11),
             Event(kind="discount", vendor="openai", topic="discount", title="OpenAI cuts GPT pricing by 50% off", url="https://e.org/2", source="a",
                   source_name="A", trust="primary", when=NOW, item_id="2", score=10, figures={"percent": "50"})]
    second = [first[0], Event(**{**first[1].__dict__, "kind": "new-model", "topic": "model", "title": "Introducing GPT-6", "item_id": "3", "figures": {}})]
    export.write_bundle(export.build_bundle(first, RULES, "2026-10-13"), tmp_path)
    old = tmp_path / "campaigns/ai-news-2026-10-13/posts/vi/03-discount.yaml"
    assert old.exists()
    export.write_bundle(export.build_bundle(second, RULES, "2026-10-13"), tmp_path, force=True)
    assert not old.exists() and (tmp_path / "campaigns/ai-news-2026-10-13/posts/vi/03-new-model.yaml").exists()
    keep = tmp_path / "campaigns/ai-news-2026-10-13/posts/vi/03-new-model.yaml"
    data = yaml.safe_load(keep.read_text(encoding="utf-8"))
    data["status"] = "ready"
    keep.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    third = [first[0], Event(**{**first[1].__dict__, "kind": "benchmark", "topic": "benchmark", "title": "GPT-6 is state-of-the-art on SWE-bench", "item_id": "4", "figures": {}})]
    with pytest.raises(export.WouldOverwrite):
        export.write_bundle(export.build_bundle(third, RULES, "2026-10-13"), tmp_path, force=True)
    assert keep.exists()


def test_12_a_mirror_item_that_points_off_the_vendor_site_is_dropped():
    feed = fixture("anthropic_news_feed.xml").replace(b"https://www.anthropic.com/news/anthropic-cyber-mission", b"https://evil.example.org/x", 1)
    urls = [i.url for i in rss.parse(feed, SOURCES["anthropic-news"])]
    assert len(urls) == 19 and all("anthropic.com" in u for u in urls)


def test_15_a_wide_encoded_feed_cannot_smuggle_a_doctype_past_the_check():
    evil = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE rss [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title><link>https://x.org/</link></item></channel></rss>'
    with pytest.raises(SourceError, match="UTF-8"):
        rss.parse(evil.encode("utf-16"), source("anthropic-news"))
    with pytest.raises(SourceError, match="UTF-8"):
        rss.parse(b'<?xml version="1.0" encoding="UTF-16"?><rss></rss>', source("anthropic-news"))


def test_7_events_not_exported_today_wait_for_a_later_issue(tmp_path):
    root = make_root(tmp_path)
    many = [Event(kind="free-promo", vendor="openai", topic="free", title=f"Codex is free for {n} days", url=f"https://e.org/{n}", source="a",
                  source_name="A", trust="primary", when=NOW, item_id=f"i{n}", score=20 - n) for n in range(8)]
    cli.save_scan(root, many, [], NOW)
    assert cli.cmd_export(argparse.Namespace(date="2026-10-13", studio=None, force=False), root) == 0
    waiting = cli.load_pending(root)
    assert {e.item_id for e in waiting} == {f"i{n}" for n in range(5, 8)}                   # max_stories is 5; the rest are kept
    assert cli.cmd_export(argparse.Namespace(date="2026-10-14", studio=None, force=False), root) == 0     # ...and become tomorrow's issue
    assert cli.load_pending(root) == []


def test_14_bad_date_is_refused_and_the_issue_date_is_the_readers_date(tmp_path):
    root = make_root(tmp_path)
    cli.save_scan(root, [Event(kind="free-promo", vendor="openai", topic="free", title="Codex is free for one week", url="https://e.org", source="a",
                               source_name="A", trust="primary", when=NOW, item_id="i", score=12)], [], NOW)
    assert cli.cmd_export(argparse.Namespace(date="../../etc", studio=None, force=False), root) == 2
    from datetime import datetime, timezone
    cli.save_scan(root, cli.load_pending(root), [], datetime(2026, 10, 13, 22, 17, tzinfo=timezone.utc))
    assert cli.cmd_export(argparse.Namespace(date=None, studio=None, force=False), root) == 0
    assert (root / "out" / "ai-news-2026-10-14").is_dir()                                   # 22:17 UTC is already the 14th in Vietnam


def test_14_export_checks_the_studio_target_before_writing_anything(tmp_path):
    root, studio = make_root(tmp_path / "r"), tmp_path / "studio"
    cli.save_scan(root, [Event(kind="free-promo", vendor="openai", topic="free", title="Codex is free for one week", url="https://e.org", source="a",
                               source_name="A", trust="primary", when=NOW, item_id="i", score=12)], [], NOW)
    (studio / "tools" / "ai-news-2026-10-13").mkdir(parents=True)
    assert cli.cmd_export(argparse.Namespace(date="2026-10-13", studio=str(studio), force=False), root) == 4
    assert not (root / "out" / "ai-news-2026-10-13").exists()                               # no half result


def test_14_help_lists_only_real_commands(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "report" not in capsys.readouterr().out


def test_1_a_kind_that_has_a_sentence_but_no_model_figure_falls_back_to_its_own_headline():
    event = Event(kind="price-drop", vendor="openai", topic="discount", title="Some vendor cuts prices", url="https://e.org", source="s",
                  source_name="S", trust="primary", when=NOW, item_id="i", score=10)          # no figures: the sentence would print blanks
    assert all(export.headline(event, lang) == "Some vendor cuts prices" for lang in ("vi", "ja", "en"))


GITHUB_RELEASE = """<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>tag:github.com,2008:Repository/1/rust-v0.50.0</id><updated>2026-10-12T10:00:00Z</updated>
<link rel="alternate" href="https://github.com/openai/codex/releases/tag/rust-v0.50.0"/><title>rust-v0.50.0</title>
<content type="html">&lt;h2&gt;What's Changed&lt;/h2&gt;&lt;ul&gt;&lt;li&gt;Added background agent mode for long tasks by @alice in https://github.com/openai/codex/pull/1&lt;/li&gt;
&lt;li&gt;feat(cli): add voice mode (#2)&lt;/li&gt;&lt;li&gt;nested list: &lt;ul&gt;&lt;li&gt;Added a sandbox profile picker&lt;/li&gt;&lt;/ul&gt;&lt;/li&gt;&lt;/ul&gt;
&lt;h2&gt;New Contributors&lt;/h2&gt;&lt;ul&gt;&lt;li&gt;@bob made their first contribution in #3&lt;/li&gt;&lt;/ul&gt;&lt;p&gt;&lt;strong&gt;Full Changelog&lt;/strong&gt;: https://github.com/openai/codex/compare/a...b&lt;/p&gt;</content></entry></feed>"""


def test_e_github_release_boilerplate_never_reaches_a_headline():
    item = rss.parse(GITHUB_RELEASE.encode(), SOURCES["codex-releases"])[0]
    assert not any("@" in b or "Full Changelog" in b or "New Contributors" in b or "What's Changed" in b for b in item.extra["bullets"])
    events = detect([item], SOURCES, RULES, NOW)
    titles = [e.title for e in events]
    assert any(t.endswith("Added background agent mode for long tasks") for t in titles)          # handle and PR link are gone
    assert any("add voice mode" in t for t in titles)                                            # conventional-commit style
    assert any(t.endswith("Added a sandbox profile picker") for t in titles)                    # a nested list item is its own bullet
    assert all("@alice" not in t and " in" != t[-3:] for t in titles)


def test_e_utf8_with_bom_and_odd_case_declarations_still_parse():
    body = b"<feed xmlns='http://www.w3.org/2005/Atom'><entry><id>1</id><link rel='alternate' href='https://github.com/a/b'/><title>t</title></entry></feed>"
    for head in (b"\xef\xbb\xbf<?xml version='1.0' encoding='UTF-8'?>", b"<?xml version='1.0' encoding='utf-8'?>", b""):
        assert [i.title for i in rss.parse(head + body, SOURCES["codex-releases"])] == ["t"]


def _carried(n, when, score=20):
    return Event(kind="free-promo", vendor="openai", topic="free", title=f"Codex is free for {n} days", url=f"https://e.org/{n}", source="a",
                 source_name="A", trust="primary", when=when, item_id=f"i{n}", score=score)


def test_7_carry_horizon_dedup_and_the_min_score_cut(tmp_path):
    root = make_root(tmp_path)
    old, fresh = _carried(1, NOW - timedelta(days=30)), _carried(2, NOW)
    weak = Event(kind="trending", vendor="other", topic="news", title="weak", url="https://e.org/3", source="hn-ai", source_name="HN",
                 trust="community", when=NOW - timedelta(days=6), item_id="i3")                  # carried, but scores under min_score
    cli.save_scan(root, [old, fresh, fresh, weak], [], NOW)                                     # a duplicate id inside the file itself
    down = lambda url, **k: (_ for _ in ()).throw(cli.FetchError("down"))
    events, _ = cli.scan(root, fetcher=down, now=NOW)
    assert sorted(e.item_id for e in events) == ["i2", "i3"]                                     # i1 is older than carry_days; i2 is not doubled
    assert next(e for e in events if e.item_id == "i3").score < RULES["export"]["min_score"]
    cli.save_scan(root, events, [], NOW)
    assert cli.cmd_export(argparse.Namespace(date="2026-10-13", studio=None, force=False), root) == 0
    assert cli.load_pending(root) == []                                                          # the weak one is not worth keeping


def test_7_a_lost_state_file_cannot_make_a_carried_event_appear_twice(tmp_path):
    from tests.helpers import REAL, fetcher_for
    root = make_root(tmp_path)
    first, _ = cli.scan(root, fetcher=fetcher_for(REAL), now=NOW)
    assert first
    cli.save_scan(root, first, [], NOW)
    (root / "data" / "state.json").unlink()                                                      # the "seen" memory is gone, last-scan is not
    second, _ = cli.scan(root, fetcher=fetcher_for(REAL), now=NOW)
    ids = [e.item_id for e in second]
    assert ids and len(ids) == len(set(ids))


def test_d_unreadable_or_foreign_post_files_are_refused_not_crashed_on_and_hand_written_drafts_survive(tmp_path):
    files = export.build_bundle([Event(kind="free-promo", vendor="openai", topic="free", title="Codex is free for one week", url="https://e.org/1",
                                       source="a", source_name="A", trust="primary", when=NOW, item_id="1", score=11)], RULES, "2026-10-13")
    export.write_bundle(files, tmp_path)
    posts = tmp_path / "campaigns/ai-news-2026-10-13/posts/vi"
    (posts / "09-mine.yaml").write_text("status: draft\nid: 09-mine\nslides: []\n", encoding="utf-8")        # the owner's own draft
    export.write_bundle(files, tmp_path, force=True)
    assert (posts / "09-mine.yaml").exists()                                                   # not made by the radar: left alone
    (posts / "01-free-promo.yaml").write_text(": : not yaml [", encoding="utf-8")
    with pytest.raises(export.WouldOverwrite, match="unreadable"):
        export.write_bundle(files, tmp_path, force=True)
