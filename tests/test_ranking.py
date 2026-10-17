"""Ranking boards (radar/sources/ranking.py): the first scan reports the board, a rerun is silent, a changed pick waits out a
cooldown, a stale or reshaped page makes no story, and the story reaches all three languages with the same figures.

Fixture: tests/fixtures/tbench_leaderboard_head.html is REAL, not synthetic: 15 rows of the Terminal-Bench 4.0 leaderboard
page as read on 2026-10-17 (the first 13 by rank, the one row whose cost is reported as 0, and the cheapest row), wrapped
in the page's own `self.__next_f.push` envelope. Tests that need a different board build one from `row()`."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from radar import cli, export
from radar.config import ConfigError, load_rules, load_sources
from radar.rank import score_events
from radar.sources import SourceError, ranking
from radar.store import State
from radar.textsafe import has_link
from tests.helpers import NOW, REAL, context, fetcher_for, fixture, make_root, source

RULES = load_rules()
STUDIO = Path(os.environ.get("STUDIO_PATH", "/home/user/tool-marketing-studio"))
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
PAGE = fixture("tbench_leaderboard_head.html")


def row(agent, model, effort, accuracy, total, trials=330, updated="2026-10-06T17:56:36+00:00"):
    entry = {"rank": 1, "status": "display", "n_trials": trials,
             "metadata": {"agent_display": {"label": agent}, "model_display": {"label": model}, "reasoning_effort": effort},
             "metrics": {"accuracy": accuracy, "n_trials": trials, "total_cost_usd": total}}
    if updated is not None:
        entry["updated_at"] = updated
    return entry


def chunk(rows, title="Terminal-Bench 4.0", board_id="board-4", updated=None) -> str:
    """One board as the page streams it: a header with an id and a title, and rows that carry that id."""
    header = {"id": board_id, "title": title, **({"updated_at": updated} if updated else {})}
    return '{"leaderboard":' + json.dumps(header) + ',"rows":' + json.dumps([{**r, "leaderboard_id": board_id} for r in rows]) + "}"


def page_of(*chunks) -> bytes:
    return ("<html><body>" + "".join('<script>self.__next_f.push([1,' + json.dumps(c) + '])</script>' for c in chunks) + "</body></html>").encode("utf-8")


def page(rows, **board) -> bytes:
    """A page the way the site sends it: the rows inside a streamed chunk."""
    return page_of(chunk(rows, **board))


def board(top_cost=1000.0, updated="2026-10-06T17:56:36+00:00"):
    """Twelve rows: a top row, a cheap row at 90% of its score, and filler. Costs are totals over 330 trials."""
    rows = [row("Codex", "Big", "max", 70.0, top_cost, updated=updated), row("Codex", "Value", "max", 63.0, top_cost * 0.1, updated=updated)]
    rows += [row("Codex", f"Filler{n}", "low", 20.0 + n, top_cost * 0.4, updated=updated) for n in range(10)]
    return rows


TB = source("terminal-bench-4")


def collect(tmp_path, raw=PAGE, now=NOW, state=None):
    state = state or State(tmp_path / "state.json")
    return ranking.collect(TB, context(tmp_path, {TB.url: raw}, now=now, state=state)), state


# ---- the parser, on the real page -------------------------------------------------------------------------------

def test_the_real_page_is_read_into_rows_with_a_cost_per_trial():
    rows, as_of = ranking.parse_tbench(PAGE, TB)
    assert len(rows) == 15 and str(as_of) == "2026-10-06"
    top = max(rows, key=lambda r: r["score"])
    assert (top["label"], top["score"]) == ("Claude Code + Opus 5.5 (max)", 64.85)
    sol = next(r for r in rows if r["label"] == "Codex + GPT-6.1 Sol (max)")
    assert sol["cost"] == pytest.approx(634.24 / 330)                        # the board's total cost over its trials


def test_a_row_whose_cost_is_reported_as_zero_is_never_comparable():
    rows, _ = ranking.parse_tbench(PAGE, TB)
    unreported = [r for r in rows if r["cost"] == 0]
    assert [r["label"] for r in unreported] == ["Muse Code + Muse Spark 1.3 (xhigh)"]       # the real page shows "$0.00" for it
    assert all(r["cost"] > 0 for r in ranking.usable(rows, TB))
    cheat = rows + [{"label": "Free lunch", "score": 99.0, "cost": 0.0}]
    top, value = ranking.pick(ranking.usable(cheat, TB), 0.85, 0.5)
    assert top["label"] != "Free lunch" and (value is None or value["label"] != "Free lunch")      # a $0.00 row must not win "cheapest"


def test_a_page_that_changed_shape_is_a_source_error_not_an_empty_board():
    with pytest.raises(SourceError, match="page format changed"):
        ranking.parse_tbench(b"<html><body>no streamed data</body></html>", TB)
    with pytest.raises(SourceError, match="no board titled"):
        ranking.parse_tbench(b'<script>self.__next_f.push([1,"{\\"rows\\":[]}"])</script>', TB)
    with pytest.raises(SourceError, match="usable row"):
        ranking.usable(ranking.parse_tbench(page(board()[:3]), TB)[0], TB)             # three rows is a page that changed, not a small board


def test_a_board_with_no_usable_update_date_anywhere_is_a_source_error_not_a_fresh_board(tmp_path):
    """A page that lost its dates must not pass for current: staleness cannot be judged, so there is no story."""
    undated = [{k: v for k, v in r.items() if k != "updated_at"} for r in board()]
    far_future = NOW + timedelta(days=400)                                               # long past any freshness window
    with pytest.raises(SourceError, match="update date"):
        ranking.collect(TB, context(tmp_path, {TB.url: page(undated)}, now=far_future))
    garbled = [row("Codex", "Big", "max", 70.0, 1000.0, updated="not a date"), row("Codex", "Value", "max", 63.0, 100.0, updated="")] + undated[2:]
    with pytest.raises(SourceError, match="update date"):
        ranking.collect(TB, context(tmp_path, {TB.url: page(garbled)}, now=far_future))


def test_the_boards_own_update_date_counts_when_the_rows_carry_none(tmp_path):
    undated = [{k: v for k, v in r.items() if k != "updated_at"} for r in board()]
    stale = context(tmp_path, {TB.url: page(undated, updated="2026-10-06T23:20:25+00:00")}, now=NOW + timedelta(days=60))
    assert ranking.collect(TB, stale)[1] == [] and "STALE" in stale.notes[TB.id]             # the header's date is enough to judge it
    fresh = context(tmp_path, {TB.url: page(undated, updated="2026-10-06T23:20:25+00:00")})
    assert [e.kind for e in ranking.collect(TB, fresh)[1]] == ["ranking-board"]


def test_a_different_default_board_is_never_published_under_the_configured_name(tmp_path):
    """The configured URL has no version in it: when the site's default board becomes another version, the scores are not 4.0's."""
    with pytest.raises(SourceError, match="no board titled 'Terminal-Bench 4.0'"):
        ranking.collect(TB, context(tmp_path, {TB.url: page(board(), title="Terminal-Bench 5.0")}))


def test_rows_come_from_the_configured_board_even_when_the_page_streams_another_first(tmp_path):
    other = chunk([row("Other", "Newer", "max", 99.0, 10.0)] * 12, title="Terminal-Bench 5.0", board_id="board-5")
    ours = chunk(board(), board_id="board-4")
    rows, _ = ranking.parse_tbench(page_of(other, ours), TB)
    assert {r["label"] for r in rows} >= {"Codex + Big (max)", "Codex + Value (max)"} and not any("Newer" in r["label"] for r in rows)
    rows, _ = ranking.parse_tbench(page_of(ours, other), TB)                              # and in the other order
    assert not any("Newer" in r["label"] for r in rows)


def test_rows_that_do_not_belong_to_the_boards_header_are_refused():
    stray = '{"leaderboard":{"id":"board-4","title":"Terminal-Bench 4.0"},"rows":' + json.dumps([{**r, "leaderboard_id": "board-9"} for r in board()]) + "}"
    with pytest.raises(SourceError, match="no rows"):
        ranking.parse_tbench(page_of(stray), TB)


def test_a_ranking_source_needs_an_expected_title(tmp_path):
    (tmp_path / "config").mkdir()
    text = (Path(__file__).resolve().parent.parent / "config" / "sources.yaml").read_text(encoding="utf-8")
    without = re.sub(r"\n[ \t]*expect_title:[^\n]*", "", text)
    assert without != text and "expect_title" not in without.split("terminal-bench-4")[1].split("live_checked")[0]      # the line really went
    (tmp_path / "config" / "sources.yaml").write_text(without, encoding="utf-8")
    with pytest.raises(ConfigError, match="expect_title"):
        load_sources(tmp_path)


def test_a_ranking_source_needs_a_known_format(tmp_path):
    (tmp_path / "config").mkdir()
    text = (Path(__file__).resolve().parent.parent / "config" / "sources.yaml").read_text(encoding="utf-8")
    (tmp_path / "config" / "sources.yaml").write_text(text.replace("format: tbench_leaderboard", "format: nonsense"), encoding="utf-8")
    with pytest.raises(ConfigError, match="format"):
        load_sources(tmp_path)


def test_every_configured_format_has_a_parser():
    from radar.config import RANKING_FORMATS
    assert set(RANKING_FORMATS) == set(ranking.FORMATS)


# ---- the comparison ---------------------------------------------------------------------------------------------

def rows_of(*specs):
    return [{"label": name, "score": score, "cost": cost} for name, score, cost in specs]


def test_the_value_pick_is_the_cheapest_row_close_enough_to_the_top():
    rows = rows_of(("top", 70, 10.0), ("close-and-cheap", 62, 2.0), ("closer-but-dearer", 68, 4.0), ("too-far", 50, 0.1))
    top, value = ranking.pick(rows, 0.85, 0.5)
    assert (top["label"], value["label"]) == ("top", "close-and-cheap")           # "too-far" is cheaper but below 85% of the top


def test_no_story_when_the_close_rows_are_not_much_cheaper_or_there_are_none():
    assert ranking.pick(rows_of(("top", 70, 10.0), ("close", 65, 8.0)), 0.85, 0.5)[1] is None       # only 20% cheaper
    assert ranking.pick(rows_of(("top", 70, 10.0), ("far", 40, 1.0)), 0.85, 0.5)[1] is None         # nothing within 85% of the top
    assert ranking.pick(rows_of(("only", 70, 10.0)), 0.85, 0.5)[1] is None


def test_on_a_tie_for_the_top_score_the_cheaper_row_is_the_top():
    top, _ = ranking.pick(rows_of(("dear", 70, 10.0), ("cheap", 70, 4.0), ("c", 20, 1.0)), 0.85, 0.5)
    assert top["label"] == "cheap"


def test_percentages_never_print_zero_for_a_nonzero_share():
    assert ranking.percent(0.004, 1.0) == "<1" and ranking.percent(13.4, 100) == "13" and ranking.percent(89.8, 100) == "90"
    assert ranking.money(0.0042) == "0.0042" and ranking.money(1.9219) == "1.92"


# ---- the story: first scan, reruns, cooldown, staleness ---------------------------------------------------------

def test_the_first_scan_reports_the_board_instead_of_recording_a_baseline(tmp_path):
    (items, events), state = collect(tmp_path)
    assert items == [] and [e.kind for e in events] == ["ranking-board"]
    event = events[0]
    assert event.figures == {"model": "Codex + GPT-6.1 Sol (max)", "benchmark": "Terminal-Bench 4.0", "value_score": "58.2%",
                             "value_cost": "1.92", "top_model": "Claude Code + Opus 5.5 (max)", "top_score": "64.8%",
                             "top_cost": "14.30", "pct_score": "90", "pct_cost": "13", "unit": "trial"}
    assert event.vendor == "other" and event.trust == "primary" and event.verified and event.topic == "ranking"
    assert event.detail == "board updated 2026-10-06" and event.url == TB.url
    assert state.snapshots[TB.id]["fingerprint"] and state.snapshots[TB.id]["announced"] == NOW.date().isoformat()


def test_the_same_board_again_is_silent(tmp_path):
    (_, first), state = collect(tmp_path)
    (_, again), _ = collect(tmp_path, now=NOW + timedelta(days=30), state=state)           # long after any cooldown
    assert len(first) == 1 and again == []


def test_the_fingerprint_names_the_two_rows_so_a_moved_score_is_not_news(tmp_path):
    (_, first), state = collect(tmp_path, page(board()))
    nudged = board()
    nudged[1]["metrics"]["accuracy"] = 63.4                                               # same two rows, a different score
    (_, again), _ = collect(tmp_path, page(nudged), now=NOW + timedelta(days=30), state=state)
    assert len(first) == 1 and again == []


def test_a_changed_pick_waits_out_the_cooldown_then_is_reported_with_a_new_id(tmp_path):
    (_, first), state = collect(tmp_path, page(board()))
    changed = board()
    changed.append(row("Codex", "Newcomer", "max", 64.0, 1000.0 * 0.05))                    # a cheaper row that is also close to the top
    (_, early), _ = collect(tmp_path, page(changed), now=NOW + timedelta(days=3), state=state)
    assert early == []                                                                     # 3 days after the last story: inside the 14-day cooldown
    (_, late), _ = collect(tmp_path, page(changed), now=NOW + timedelta(days=15), state=state)
    assert [e.figures["model"] for e in late] == ["Codex + Newcomer (max)"]
    assert late[0].item_id != first[0].item_id


def test_the_cooldown_note_is_in_the_run_report(tmp_path):
    (_, _), state = collect(tmp_path, page(board()))
    changed = board() + [row("Codex", "Newcomer", "max", 64.0, 50.0)]
    ctx = context(tmp_path, {TB.url: page(changed)}, now=NOW + timedelta(days=3), state=state)
    ranking.collect(TB, ctx)
    assert "cooldown" in ctx.notes[TB.id] and "no story yet" in ctx.notes[TB.id]


def test_a_replay_of_a_scan_that_was_never_saved_has_the_same_id_and_a_later_return_to_an_earlier_pair_is_new(tmp_path):
    (_, one), _ = collect(tmp_path, page(board()))
    (_, replay), _ = collect(tmp_path, page(board()))                                      # the state was lost: a fresh State
    assert one[0].item_id == replay[0].item_id                                             # so the pending copy de-duplicates it
    (_, first), state = collect(tmp_path, page(board()))
    other = board() + [row("Codex", "Newcomer", "max", 64.0, 50.0)]
    (_, second), state = collect(tmp_path, page(other), now=NOW + timedelta(days=20), state=state)
    later = NOW + timedelta(days=40)                                                       # a live board has a newer update by then
    (_, back), _ = collect(tmp_path, page(board(updated=later.isoformat())), now=later, state=state)
    assert len({first[0].item_id, second[0].item_id, back[0].item_id}) == 3


def test_a_board_not_updated_for_too_long_makes_no_story_and_leaves_the_state_alone(tmp_path):
    ctx = context(tmp_path, {TB.url: PAGE}, now=NOW + timedelta(days=60))                   # the page's newest update is 2026-10-06
    items, events = ranking.collect(TB, ctx)
    assert events == [] and "STALE" in ctx.notes[TB.id] and TB.id not in ctx.state.snapshots


def test_a_board_without_a_value_pick_makes_no_story(tmp_path):
    flat = [row("Codex", f"M{n}", "max", 60.0 - n, 400.0) for n in range(12)]               # nothing close to the top is cheaper
    ctx = context(tmp_path, {TB.url: page(flat)})
    assert ranking.collect(TB, ctx)[1] == [] and "no story" in ctx.notes[TB.id] and TB.id not in ctx.state.snapshots


# ---- through the pipeline ---------------------------------------------------------------------------------------

def ranked(tmp_path, raw=PAGE):
    (_, events), _ = collect(tmp_path, raw)
    return score_events(events, RULES, NOW)


def test_the_english_headline_is_the_collectors_title_and_vi_ja_state_every_figure(tmp_path):
    event = ranked(tmp_path)[0]
    assert export.headline(event, "en") == event.title
    for lang in ("vi", "ja"):
        text = export.headline(event, lang)
        for key in ("value_score", "value_cost", "top_score", "top_cost", "pct_score", "pct_cost"):
            assert event.figures[key].rstrip("%") in text, (lang, key)
        assert event.figures["model"] in text and event.figures["top_model"] in text


def test_the_story_exports_in_three_languages_as_a_draft_with_the_same_numbers_and_no_links(tmp_path):
    files = export.build_bundle(ranked(tmp_path), RULES, "2026-10-13")
    slug = "ai-news-2026-10-13"
    per_lang = {}
    for lang in ("vi", "ja", "en"):
        data = yaml.safe_load(files[f"campaigns/{slug}/posts/{lang}/01-ranking-board.yaml"])
        assert data["status"] == "draft"
        blob = yaml.dump(data["slides"], allow_unicode=True) + data["platforms"]["facebook"]["caption"]
        assert not has_link(blob)
        per_lang[lang] = set(NUMBER.findall(blob))
    assert per_lang["vi"] == per_lang["ja"] == per_lang["en"]
    campaign = yaml.safe_load(files[f"campaigns/{slug}/campaign.yaml"])
    assert campaign["links"]["s1"] == TB.url


def test_two_different_rows_are_never_drawn_as_a_price_that_fell(tmp_path):
    files = export.build_bundle(ranked(tmp_path), RULES, "2026-10-13")
    for lang in ("vi", "ja", "en"):
        post = yaml.safe_load(files[f"campaigns/ai-news-2026-10-13/posts/{lang}/01-ranking-board.yaml"])
        stat = next(s for s in post["slides"] if s["layout"] == "stat")
        assert "before" not in stat and "after" not in stat          # the studio draws `before` struck through: a price cut, which this is not
        assert stat["value"] == "13%"


def test_the_post_says_what_the_cost_is_and_that_a_row_is_not_a_model_alone(tmp_path):
    files = export.build_bundle(ranked(tmp_path), RULES, "2026-10-13")
    brief = yaml.safe_load(files["tools/ai-news-2026-10-13/brief.yaml"])
    assert any("not a price list" in line for line in brief["limitations"])
    for lang in ("vi", "ja", "en"):
        post = yaml.safe_load(files[f"campaigns/ai-news-2026-10-13/posts/{lang}/01-ranking-board.yaml"])
        facts = next(s for s in post["slides"] if s["layout"] == "features")["items"]
        assert any(f["text"] == export.TEXT[lang]["ranking_note"] for f in facts)
    drop = export.Event(kind="price-drop", vendor="openai", topic="discount", title="Acme Alpha input price drops 40%", url="https://example.org/p",
                        source="s-p", source_name="Some source", trust="primary", when=NOW, item_id="i-p", score=12.0,
                        figures={"model": "Acme Alpha", "percent": "40", "old_price": "5.00", "new_price": "3.00"})
    plain = yaml.safe_load(export.build_bundle([drop], RULES, "2026-10-14")["tools/ai-news-2026-10-14/brief.yaml"])
    assert not any("not a price list" in line for line in plain["limitations"])        # a ranking caveat only travels with a ranking story


def test_a_hook_is_never_cut_inside_a_figure():
    text = "Terminal-Bench 4.0: Codex + GPT-6.1 Sol (max) scores 58.2%, 90% of the top score (Claude Code + Opus 5.5 (max), 64.8%) for 13%"
    cut_inside = text.index("64.8") + 2                                    # a limit that lands between "64" and ".8"
    clipped = export.clip(text, cut_inside + 1)
    assert clipped.endswith("…") and "64" not in clipped.split("Opus")[-1]
    assert export.clip("short", 125) == "short"


@pytest.mark.parametrize("lang", ["vi", "ja", "en"])
def test_the_ranking_hook_is_short_complete_and_inside_the_limit(tmp_path, lang):
    files = export.build_bundle(ranked(tmp_path), RULES, "2026-10-13")
    post = yaml.safe_load(files[f"campaigns/ai-news-2026-10-13/posts/{lang}/01-ranking-board.yaml"])
    hook = post["platforms"]["facebook"]["caption"].split("\n")[0]
    assert len(hook) <= 125 and "…" not in hook and "90%" in hook and "13%" in hook


def test_ranking_events_never_merge_with_a_vendor_story(tmp_path):
    from radar.rank import same_story
    event = ranked(tmp_path)[0]
    other = export.Event(kind="new-model", vendor="openai", topic="ranking", title="Codex GPT-6.1 Sol", url="https://example.org/x",
                         source="s2", source_name="S2", trust="primary", when=NOW)
    assert not same_story(event, other) and event.vendor == "other"


# ---- the whole run ----------------------------------------------------------------------------------------------

def test_a_full_scan_reports_the_board_once_and_names_it_in_the_run_report(tmp_path):
    root = make_root(tmp_path)
    first, reports = cli.scan(root, fetcher=fetcher_for(REAL), now=NOW)
    note = next(r for r in reports if r.id == "terminal-bench-4")
    assert note.ok and "value pick Codex + GPT-6.1 Sol (max)" in note.note
    assert sum(e.kind == "ranking-board" for e in first) == 1
    cli.save_scan(root, first, reports, NOW)
    second, reports2 = cli.scan(root, fetcher=fetcher_for(REAL), now=NOW + timedelta(hours=1))
    assert sum(e.kind == "ranking-board" for e in second) == 1                              # still pending, carried once, never duplicated
    assert "same top and value pick" in next(r for r in reports2 if r.id == "terminal-bench-4").note


@pytest.mark.skipif(not (STUDIO / "mkt" / "check.py").exists(), reason="needs a tool-marketing-studio checkout (STUDIO_PATH)")
def test_the_ranking_issue_passes_the_studios_check_with_no_error_and_no_warning(tmp_path, capsys):
    root = make_root(tmp_path)
    events, reports = cli.scan(root, fetcher=fetcher_for(REAL), now=NOW)
    cli.save_scan(root, events, reports, NOW)
    assert cli.cmd_export(argparse.Namespace(date="2026-10-13", studio=None, force=False), root) == 0
    assert any(p.name.endswith("ranking-board.yaml") for p in (root / "out" / "ai-news-2026-10-13").rglob("*.yaml"))
    capsys.readouterr()
    code = cli.cmd_verify(argparse.Namespace(studio=str(STUDIO), slug="ai-news-2026-10-13"), root)
    out = capsys.readouterr().out
    assert code == 0 and "0 error(s), 0 warning(s)" in out, out
