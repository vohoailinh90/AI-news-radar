from datetime import timedelta

from radar.config import load_rules, load_sources
from radar.detect import detect
from radar.models import Item
from radar.rank import score_events
from radar.sources import changelog, hn, rss
from tests.helpers import NOW, fixture, source

RULES = load_rules()
SOURCES = {s.id: s for s in load_sources()}


def _item(source_id, title, body="", points=0, published=NOW):
    return Item(id=f"{source_id}:{title}", source=source_id, title=title, url="https://example.org/x",
                published=published, body=body, points=points)


def kinds(items):
    return [e.kind for e in detect(items, SOURCES, RULES, NOW)]


def test_action_phrases_fire_and_bare_nouns_do_not():
    assert kinds([_item("anthropic-news", "Weekly usage limits have been reset for all Claude plans")]) == ["quota-reset"]
    assert kinds([_item("anthropic-news", "We doubled the weekly usage limits")]) == ["usage-limit-change"]
    assert kinds([_item("openai-news", "Codex is free for one week on every plan")]) == ["free-promo"]
    assert kinds([_item("openai-news", "OpenAI cuts GPT pricing by 50% off for batch jobs")]) == ["discount"]
    assert kinds([_item("openai-news", "Introducing GPT-6")]) == ["new-model"]
    # nouns alone are not actions
    assert kinds([_item("anthropic-news", "How we think about usage limits")]) == []
    assert kinds([_item("anthropic-news", "Free speech and AI policy")]) == []
    assert kinds([_item("anthropic-news", "Resetting your password")]) == []


def test_vendor_comes_from_the_text_then_the_source():
    event = detect([_item("hn-ai", "Gemini 3 launches with a one million token window", points=50)], SOURCES, RULES, NOW)[0]
    assert event.vendor == "google"
    event = detect([_item("openai-news", "Introducing a new thing")], SOURCES, RULES, NOW)[0]
    assert event.vendor == "openai"


def test_community_story_without_a_rule_needs_enough_points_to_trend():
    assert kinds([_item("hn-ai", "My weekend project got a lot of attention", points=120)]) == []
    assert kinds([_item("hn-ai", "My weekend project got a lot of attention", points=450)]) == ["trending"]
    assert kinds([_item("hn-ai", "Show HN: I built a tool to reset Docker rate limits", points=900)]) == []   # Show HN is never news


def test_percent_figure_is_extracted_for_discounts():
    event = detect([_item("openai-news", "Prices drop: 30% off all Codex plans this month")], SOURCES, RULES, NOW)[0]
    assert event.kind == "discount" and event.figures == {"percent": "30"}


def test_changelog_fixed_bullets_env_vars_and_ux_notes_are_not_news():
    bullets = ["Fixed the usage limit alert repeating once per background agent",
               "Added `CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL` to run every workflow agent on one model",
               "Improved the notice shown when a usage limit is reached; it says when the limit resets",
               "Added a plugin marketplace browser to the /plugin screen",
               "Added managed policies for the agent gateway"]
    item = Item(id="claude-code-changelog:9.9.9", source="claude-code-changelog", title="Claude Code changelog 9.9.9",
                url="https://example.org", body="", extra={"version": "9.9.9", "bullets": bullets})
    events = detect([item], SOURCES, RULES, NOW)
    assert [e.kind for e in events] == ["new-feature"]
    assert "marketplace browser" in events[0].title


def test_changelog_picks_at_most_three_features_per_version():
    bullets = [f"Added a new agent mode number {n}" for n in range(8)]
    item = Item(id="c:1", source="claude-code-changelog", title="t", url="https://example.org", extra={"version": "1.0.0", "bullets": bullets})
    assert len(detect([item], SOURCES, RULES, NOW)) == 3


def test_real_changelog_and_feed_through_detect(tmp_path):
    from tests.helpers import context
    ctx = context(tmp_path, {SOURCES["claude-code-changelog"].url: fixture("claude_code_changelog_head.md"),
                             SOURCES["anthropic-news"].url: fixture("anthropic_news_feed.xml")})
    items = changelog.collect(SOURCES["claude-code-changelog"], ctx)[0] + rss.collect(SOURCES["anthropic-news"], ctx)[0]
    events = detect(items, SOURCES, RULES, NOW)
    assert {e.kind for e in events} <= {"new-feature", "announcement"} and events
    assert all("`" not in e.title for e in events)


def _ev(title, source, trust, vendor="anthropic", topic="quota", item_id="i"):
    from radar.models import Event
    return Event(kind="quota-reset", vendor=vendor, topic=topic, url="https://e.org", when=NOW, source=source, source_name=source,
                 trust=trust, title=title, item_id=item_id)


def test_ranking_prefers_newer_and_corroborated():
    from radar.models import Event
    base = dict(vendor="anthropic", topic="quota", url="https://example.org", when=NOW)
    fresh = Event(kind="quota-reset", source="a", source_name="A", trust="primary", title="fresh", **base)
    old = Event(kind="quota-reset", source="b", source_name="B", trust="primary", title="old", **{**base, "when": NOW - timedelta(days=5)})
    assert [e.title for e in score_events([old, fresh], RULES, NOW)] == ["fresh", "old"]
    rumour = _ev("Anthropic resets weekly usage limits", "hn-ai", "community", item_id="hn:1")
    official = _ev("Weekly usage limits have been reset for Claude plans", "anthropic-news", "primary")
    alone = score_events([_ev("Anthropic resets weekly usage limits", "hn-ai", "community", item_id="hn:1")], RULES, NOW)[0]
    paired = {e.source: e for e in score_events([rumour, official], RULES, NOW)}
    assert paired["hn-ai"].score > alone.score and paired["hn-ai"].corroborated_by == ["anthropic-news"]
    assert paired["hn-ai"].verified and not alone.verified          # a primary source corroborating upgrades it


def test_corroboration_needs_the_same_story_not_just_the_same_vendor_and_topic():
    official = _ev("Introducing Claude Opus 9", "anthropic-news", "primary", topic="model")
    wrapper = _ev("I released a Claude wrapper for my team", "hn-ai", "community", topic="model", item_id="hn:2")
    assert not {e.source: e for e in score_events([wrapper, official], RULES, NOW)}["hn-ai"].verified
    # an unknown vendor matches anything, so it never corroborates
    free_hn = _ev("Free credits for my startup", "hn-ai", "community", vendor="other", topic="free", item_id="hn:3")
    free_or = _ev("Free credits for my startup", "openrouter-models", "primary", vendor="other", topic="free")
    assert not {e.source: e for e in score_events([free_hn, free_or], RULES, NOW)}["hn-ai"].verified


def test_rescoring_a_carried_event_forgets_an_old_corroboration():
    rumour = _ev("Anthropic resets weekly usage limits", "hn-ai", "community", item_id="hn:1")
    rumour.corroborated_by = ["Anthropic news"]
    assert score_events([rumour], RULES, NOW)[0].corroborated_by == [] and not rumour.verified


def test_ranking_is_deterministic():
    from radar.models import Event
    events = [Event(kind="new-model", vendor="openai", topic="model", url="https://e.org", source="s", source_name="S", trust="primary",
                    when=NOW, title=f"t{n}") for n in range(5)]
    assert [e.title for e in score_events(list(events), RULES, NOW)] == [e.title for e in score_events(list(reversed(events)), RULES, NOW)]


def test_exclude_rules_drop_fix_notes_even_when_an_action_phrase_matches():
    assert kinds([_item("anthropic-news", "Fixed: weekly usage limits reset bug")]) == []
    bullets = ["Fixed a bug that doubled the weekly usage limits", "Raised the weekly usage limits for all plans"]
    item = Item(id="c:2", source="claude-code-changelog", title="t", url="https://example.org", extra={"version": "1.0.1", "bullets": bullets})
    events = detect([item], SOURCES, RULES, NOW)
    assert [e.kind for e in events] == ["usage-limit-change"] and "Raised" in events[0].title


def test_primary_source_outranks_the_same_story_from_the_community():
    from radar.models import Event
    base = dict(kind="new-model", vendor="openai", topic="model", url="https://e.org", when=NOW, source_name="S", title="t")
    primary, community = (score_events([Event(source="s", trust=trust, item_id="i", **base)], RULES, NOW, {"i": 0})[0].score
                          for trust in ("primary", "community"))
    assert primary - community == RULES["scoring"]["trust_bonus"]["primary"]


def test_quota_reset_phrase_inside_a_changelog_bullet_is_ux_not_a_reset_announcement():
    bullets = ["Added a banner that shows when your weekly usage limit reset happens"]
    item = Item(id="c:3", source="claude-code-changelog", title="t", url="https://example.org", extra={"version": "1.0.2", "bullets": bullets})
    assert [e.kind for e in detect([item], SOURCES, RULES, NOW)] == []
    # the same words in a news headline ARE an announcement
    assert kinds([_item("anthropic-news", "Your weekly usage limit reset happens early this week")]) == ["quota-reset"]
