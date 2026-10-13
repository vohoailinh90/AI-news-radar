"""A small hand-labelled corpus: headline shapes (written from what real announcements look like, not copied
from live feeds) and the event each must produce. Precision rows (None) matter as much as recall rows."""

import pytest

from radar.config import load_rules, load_sources
from radar.detect import detect
from radar.models import Item
from tests.helpers import NOW

RULES = load_rules()
SOURCES = {s.id: s for s in load_sources()}

# (source, headline, expected kind or None)
CORPUS = [
    # quota reset: the verb forms and word orders announcements use
    ("anthropic-news", "Anthropic resets usage limits for Claude Code", "quota-reset"),
    ("anthropic-news", "Resets usage limits for all Max users", "quota-reset"),
    ("openai-news", "OpenAI resets Codex usage limits after bug", "quota-reset"),
    ("openai-news", "Codex limits reset", "quota-reset"),
    ("openai-news", "Codex usage reset for all Plus and Pro users", "quota-reset"),
    ("anthropic-news", "Weekly usage limits have been reset for all Claude plans", "quota-reset"),
    ("hn-ai", "Anthropic resets everyone's weekly usage limits", "quota-reset"),
    # ...and what only looks like one
    ("hn-ai", "Show HN: I built a tool to reset Docker rate limits", None),
    ("hn-ai", "A tool to reset Docker rate limits", None),
    ("anthropic-news", "Our policy on usage limits: no reset after 30 days", None),
    ("anthropic-news", "How we think about usage limits", None),
    ("anthropic-news", "Resetting your password", None),
    # usage limit changes
    ("anthropic-news", "Claude Code usage limits increased", "usage-limit-change"),
    ("openai-news", "Codex: 2x rate limits through Dec 31", "usage-limit-change"),
    ("anthropic-news", "Anthropic raises limits on Claude Code", "usage-limit-change"),
    ("anthropic-news", "We doubled the weekly usage limits", "usage-limit-change"),
    ("hn-ai", "Reduced the frequency of rate limit errors when running many subagents", None),
    # free / discounts
    ("openai-news", "Codex is free for one week on every plan", "free-promo"),
    ("openai-news", "GPT-5 mini is available for free until Friday", "free-promo"),
    ("openai-news", "OpenAI cuts GPT pricing by 50% off for batch jobs", "discount"),
    ("hn-ai", "Is free speech dead? Why are free users banned", None),
    ("hn-ai", "Free credits for my startup", None),
    ("hn-ai", "New technique reduces latency and cost for LLM inference", None),
    # models
    ("openai-news", "Introducing GPT-6", "new-model"),
    ("anthropic-news", "Introducing Claude Opus 9", "new-model"),
    ("hn-ai", "Gemini 3 launches with a one million token window", "new-model"),
    ("anthropic-news", "Claude is now available in Microsoft Foundry", None),
    ("anthropic-news", "Introducing Claude Code security review", "announcement"),   # an announcement, not a model
    # benchmarks
    ("openai-news", "GPT-6 is state-of-the-art on SWE-bench Verified", "benchmark"),
    ("hn-ai", "Claude Code is state-of-the-art for my todo app", None),
]


@pytest.mark.parametrize("source_id,headline,expected", CORPUS)
def test_headline_shape(source_id, headline, expected):
    item = Item(id=f"{source_id}:{headline}", source=source_id, title=headline, url="https://example.org/x", published=NOW, points=50)
    got = [e.kind for e in detect([item], SOURCES, RULES, NOW)]
    assert got == ([expected] if expected else []), headline
