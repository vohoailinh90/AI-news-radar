import re
from datetime import timedelta

import pytest
import yaml

from radar import export
from radar.config import load_rules
from radar.models import Event
from radar.textsafe import defang, has_link
from tests.helpers import NOW

RULES = load_rules()
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def ev(kind, title, topic, trust="primary", figures=None, vendor="openai", score=10.0, **kw):
    return Event(kind=kind, vendor=vendor, topic=topic, title=title, url=f"https://example.org/{kind}", source="s-" + kind,
                 source_name="Some source", trust=trust, when=NOW, figures=figures or {}, item_id="i-" + kind, score=score, **kw)


def stories():
    return [
        ev("price-drop", "Acme Alpha input price drops 40%: $5.00 to $3.00 per 1M tokens", "discount",
           figures={"model": "Acme Alpha", "percent": "40", "old_price": "5.00", "new_price": "3.00"}, score=12),
        ev("benchmark-new-leader", "x", "benchmark", figures={"model": "gpt-5 (high)", "score": "88", "benchmark": "Aider polyglot"}, score=11),
        ev("quota-reset", "Weekly usage limits have been reset for Claude plans", "quota", trust="community", vendor="anthropic", score=9),
    ]


def load(files, path):
    return yaml.safe_load(files[path])


def test_bundle_matches_the_studio_tree_and_is_all_drafts():
    files = export.build_bundle(stories(), RULES, "2026-10-13")
    slug = "ai-news-2026-10-13"
    assert f"tools/{slug}/brief.yaml" in files and f"campaigns/{slug}/campaign.yaml" in files
    for lang in ("vi", "ja", "en"):
        assert f"tools/{slug}/positioning/{lang}.yaml" in files
        assert {p for p in files if p.startswith(f"campaigns/{slug}/posts/{lang}/")} == {
            f"campaigns/{slug}/posts/{lang}/{n}.yaml" for n in ("01-digest", "02-price-drop", "03-benchmark-new-leader")}
    for path, text in files.items():
        if "/posts/" in path:
            data = yaml.safe_load(text)
            assert data["status"] == "draft" and data["format"] == "portrait"


def test_every_language_states_the_same_numbers():
    files = export.build_bundle(stories(), RULES, "2026-10-13")
    for slot in ("01-digest", "02-price-drop", "03-benchmark-new-leader"):
        per_lang = {lang: set(NUMBER.findall(files[f"campaigns/ai-news-2026-10-13/posts/{lang}/{slot}.yaml"])) for lang in ("vi", "ja", "en")}
        assert per_lang["vi"] == per_lang["ja"] == per_lang["en"], slot


def test_posts_contain_no_link_looking_text_and_links_live_in_the_campaign():
    files = export.build_bundle(stories(), RULES, "2026-10-13")
    campaign = load(files, "campaigns/ai-news-2026-10-13/campaign.yaml")
    assert set(campaign["links"]) == {"s1", "s2", "s3"} and all(u.startswith("https://") for u in campaign["links"].values())
    for path, text in files.items():
        if "/posts/" in path:
            post = yaml.safe_load(text)
            blob = yaml.dump({k: post[k] for k in ("series", "slides")}, allow_unicode=True) + post["platforms"]["facebook"]["caption"] + "".join(post["platforms"]["facebook"]["hashtags"])
            assert not has_link(blob), path
            assert "{link}" in post["platforms"]["facebook"]["first_comment"] and post["link"] in campaign["links"]
            assert "{link}" not in blob


def test_numbers_in_posts_come_from_the_brief():
    files = export.build_bundle(stories(), RULES, "2026-10-13")
    brief_numbers = {re.sub(r"[.,]", "", n) for n in NUMBER.findall(files["tools/ai-news-2026-10-13/brief.yaml"])}
    for path, text in files.items():
        if "/posts/" in path:
            post = yaml.safe_load(text)
            stated = {re.sub(r"[.,]", "", n) for n in NUMBER.findall(yaml.dump(post["slides"], allow_unicode=True) + post["platforms"]["facebook"]["caption"])}
            assert stated - brief_numbers - {str(n) for n in range(11)} == set(), path


def test_community_story_is_labelled_unverified_everywhere():
    files = export.build_bundle(stories(), RULES, "2026-10-13")
    brief = load(files, "tools/ai-news-2026-10-13/brief.yaml")
    assert [c["verified"] for c in brief["claims"]] == [True, True, False]
    digest = load(files, "campaigns/ai-news-2026-10-13/posts/en/01-digest.yaml")
    assert any(s["layout"] == "checklist" and s["tone"] == "bad" for s in digest["slides"])
    ranked = sorted([stories()[2], stories()[0]], key=lambda e: -e.score)       # export expects ranked events
    only = export.build_bundle(ranked, RULES, "2026-10-14")
    base = "campaigns/ai-news-2026-10-14/posts/vi/"
    has_warning = lambda path: any(s["layout"] == "checklist" for s in yaml.safe_load(only[base + path])["slides"])
    assert has_warning("01-digest.yaml") and has_warning("03-quota-reset.yaml")
    assert not has_warning("02-price-drop.yaml")                                 # the verified story carries no warning slide


def test_corroborated_community_story_counts_as_verified():
    e = ev("quota-reset", "t", "quota", trust="community", corroborated_by=["Anthropic news"])
    assert e.verified and not ev("quota-reset", "t", "quota", trust="community").verified


def test_one_story_exports_a_spotlight_only_and_none_exports_nothing():
    files = export.build_bundle(stories()[:1], RULES, "2026-10-13")
    assert not any("01-digest" in p for p in files) and any("01-price-drop" in p for p in files)
    with pytest.raises(export.NothingToPost):
        export.build_bundle([ev("new-model", "t", "model", score=1.0)], RULES, "2026-10-13")


def test_defang_removes_domains_urls_and_backticks():
    assert defang("Changed usage limit messages to write claude.ai settings links") == "Changed usage limit messages to write claude settings links"
    assert defang("see https://example.com/a?b=1 now") == "see now"
    assert defang("run `claude --resume`") == "run claude --resume"
    assert not has_link(defang("visit www.example.org or openrouter.ai today"))
    assert defang("x" * 1000).endswith("…") and len(defang("x" * 1000)) <= 400


def test_write_bundle_never_overwrites(tmp_path):
    files = export.build_bundle(stories(), RULES, "2026-10-13")
    export.write_bundle(files, tmp_path)
    with pytest.raises(export.WouldOverwrite):
        export.write_bundle(files, tmp_path)                                  # exists, no --force
    export.write_bundle(files, tmp_path, force=True)                          # a draft may be replaced with --force
    post = tmp_path / "campaigns/ai-news-2026-10-13/posts/vi/01-digest.yaml"
    data = yaml.safe_load(post.read_text(encoding="utf-8"))
    data["status"] = "ready"
    post.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(export.WouldOverwrite, match="ready"):
        export.write_bundle(files, tmp_path, force=True)                      # the owner approved it: hands off
    assert yaml.safe_load(post.read_text(encoding="utf-8"))["status"] == "ready"
