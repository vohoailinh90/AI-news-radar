"""Price-drop noise seen in the 2026-10-15..19 scans: "~...-latest" aliases counted as cuts, DeepSeek taking every spotlight by
winning the tie on its name, and "$0.02 to $0.02". The OpenRouter rows are synthetic: the shape of the model list, not a recording."""

import json
import random
from datetime import timedelta

from radar import export
from radar.config import load_rules
from radar.models import Event
from radar.rank import cut_percent, score_events
from radar.sources import openrouter
from radar.store import State
from tests.helpers import NOW, context, source

RULES = load_rules()


def row(model_id, per_million, **extra):
    return {"id": model_id, "name": model_id.split("/")[-1], "created": 1, **extra,
            "pricing": {"prompt": str(per_million / 1_000_000), "completion": "0.000001"}}


def drops(tmp_path, before, after):
    src = source("openrouter-models")
    state = State(tmp_path / "s.json")
    for rows in (before, after):                       # the first scan is the baseline and reports nothing
        events = openrouter.collect(src, context(tmp_path, {src.url: json.dumps({"data": rows}).encode()}, state=state))[1]
    return {e.figures["model"]: e for e in events if e.kind == "price-drop"}


def test_an_alias_that_follows_its_target_down_is_not_a_second_price_cut(tmp_path):
    target = {"name": "Alpha 0731", "slug": "acme/alpha-0731"}
    before = [row("acme/alpha-0731", 1.0), row("~acme/alpha-latest", 1.0, alias_target=target)]
    after = [row("acme/alpha-0731", 0.5), row("~acme/alpha-latest", 0.5, alias_target=target)]
    assert set(drops(tmp_path, before, after)) == {"alpha-0731"}


def test_an_alias_that_is_redirected_to_a_cheaper_model_is_not_a_price_cut(tmp_path):
    before = [row("~acme/beta-latest", 2.5, alias_target={"name": "Beta 1", "slug": "acme/beta-1"})]
    after = [row("~acme/beta-latest", 0.8, alias_target={"name": "Beta 2", "slug": "acme/beta-2"})]
    assert drops(tmp_path, before, after) == {}


def test_a_tilde_id_marks_an_alias_even_without_alias_target(tmp_path):
    assert drops(tmp_path, [row("~acme/gamma-latest", 1.0)], [row("~acme/gamma-latest", 0.5)]) == {}


def test_an_ordinary_model_still_reports_its_cut_and_the_snapshot_marks_only_aliases(tmp_path):
    assert set(drops(tmp_path, [row("acme/delta", 1.0)], [row("acme/delta", 0.5)])) == {"delta"}
    snapshot = openrouter.snapshot_of(json.dumps({"data": [row("acme/delta", 1.0), row("~acme/delta-latest", 1.0)]}).encode(), source("openrouter-models"))
    assert snapshot["acme/delta"].get("alias") is None and snapshot["~acme/delta-latest"]["alias"] is True


def test_a_cut_that_would_print_the_same_price_twice_shows_more_digits(tmp_path):
    event = drops(tmp_path, [row("acme/epsilon", 0.0248)], [row("acme/epsilon", 0.0188)])["epsilon"]
    assert event.figures["percent"] == "24" and event.figures["old_price"] != event.figures["new_price"]
    assert "$0.0248 to $0.0188" in event.title and "$0.02 to $0.02" not in event.title


def test_price_pair_keeps_two_decimals_when_they_tell_the_prices_apart_and_adds_digits_when_they_do_not():
    assert openrouter.price_pair(5e-6, 3e-6) == ("5.00", "3.00")
    pair = openrouter.price_pair(4e-11, 3e-11)         # $0.00004 and $0.00003 per 1M tokens
    assert pair[0] != pair[1] and float(pair[0]) > float(pair[1]) > 0


def cut(name, percent, vendor="other"):
    return Event(kind="price-drop", vendor=vendor, topic="discount", title=f"{name} input price drops {percent}% on OpenRouter",
                 url="https://openrouter.ai/x", source="openrouter-models", source_name="OpenRouter model list", trust="primary", when=NOW,
                 figures={"model": name, "percent": str(percent)}, item_id=f"openrouter-models:price-drop:{name}@g1")


CUTS = [cut("DeepSeek: V4", 35, "deepseek"), cut("Meta: Llama 3.3", 83, "meta"), cut("Z: GLM 5.3 Flash", 79), cut("Qwen: 3.5", 25, "qwen")]


def test_fresh_price_cuts_with_the_same_score_are_ordered_by_size_not_by_name():
    ranked = score_events(list(CUTS), RULES, NOW)
    assert len({e.score for e in ranked}) == 1                       # the premise: only the title used to tell them apart
    assert [e.figures["percent"] for e in ranked] == ["83", "79", "35", "25"]


def test_the_order_of_equal_cuts_does_not_depend_on_the_order_they_arrive_in():
    expected = [e.item_id for e in score_events(list(CUTS) + [cut("Acme: Same 35", 35)], RULES, NOW)]
    assert expected.index("openrouter-models:price-drop:Acme: Same 35@g1") < expected.index("openrouter-models:price-drop:DeepSeek: V4@g1")   # equal size: the title decides
    for seed in range(5):
        events = list(CUTS) + [cut("Acme: Same 35", 35)]
        random.Random(seed).shuffle(events)
        assert [e.item_id for e in score_events(events, RULES, NOW)] == expected


def free_model(name):
    return Event(kind="free-model-added", vendor="other", topic="free", title=f"{name} is available free of charge on OpenRouter",
                 url="https://openrouter.ai/x", source="openrouter-models", source_name="OpenRouter model list", trust="primary", when=NOW,
                 figures={"model": name}, item_id=f"openrouter-models:free-model-added:{name}@g1")


def test_a_tied_event_that_is_not_a_price_cut_keeps_its_place_between_the_cuts():
    ranked = score_events(list(CUTS) + [free_model("Nemo: Free")], RULES, NOW)       # by title: DeepSeek, Meta, Nemo, Qwen, Z
    assert len({e.score for e in ranked}) == 1
    assert [e.figures["model"] for e in ranked] == ["Meta: Llama 3.3", "Z: GLM 5.3 Flash", "Nemo: Free", "DeepSeek: V4", "Qwen: 3.5"]


def test_a_bigger_cut_does_not_jump_over_an_event_that_scores_higher():
    older = cut("Old: Huge", 99)
    older.when = NOW - timedelta(hours=72)
    ranked = score_events([older, cut("New: Small", 20)], RULES, NOW)
    assert [e.figures["model"] for e in ranked] == ["New: Small", "Old: Huge"]


def test_the_spotlights_go_to_the_biggest_cuts():
    stories = export.select(score_events(list(CUTS), RULES, NOW), RULES)
    assert [e.figures["percent"] for e in stories[: RULES["export"]["spotlights"]]] == ["83", "79"]


def test_cut_percent_is_zero_for_anything_that_is_not_a_price_cut_with_a_number():
    other = cut("X", 90)
    other.kind = "model-listed"
    broken = cut("Y", 90)
    broken.figures["percent"] = "n/a"
    missing = cut("Z", 90)
    del missing.figures["percent"]
    assert [cut_percent(e) for e in (other, broken, missing)] == [0, 0, 0]
