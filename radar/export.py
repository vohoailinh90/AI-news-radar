"""Ranked Events -> a bundle laid out exactly like tool-marketing-studio's tree, one tool+campaign pair
per issue (`ai-news-YYYY-MM-DD`). Posts are filled from radar/templates.py and Event.figures, so every
number in a post is the same string that sits in the brief, in all three languages. Status is always
`draft`: approving and publishing are the studio's, and the owner's."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import yaml

from .models import COMMUNITY, PRIMARY, Event
from .rank import corroborators, same_story
from .templates import KIND_EMOJI, POSITIONING, TEXT
from .textsafe import defang, has_link

LANGS = ("vi", "ja", "en")
RANKING_FIGURES = ("value_score", "value_cost", "top_model", "top_score", "top_cost", "pct_score", "pct_cost", "unit")


class NothingToPost(RuntimeError):
    pass


class WouldOverwrite(RuntimeError):
    pass


def dump(data: dict) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)


def trust_label(event: Event, t: dict) -> str:
    if event.trust != COMMUNITY:
        return t["official"]
    return t["corroborated"] if event.corroborated_by else t["community"]


def headline(event: Event, lang: str) -> str:
    sentence = TEXT[lang]["sentences"].get(event.kind)
    if sentence is None or not event.figures.get("model"):
        return event.title                 # a rule-detected event has no model figure: its own headline is the fact
    f = {"percent": "", "old_price": "", "new_price": "", "score": "", **dict.fromkeys(RANKING_FIGURES, ""), **event.figures,
         "model": defang(event.figures.get("model", "")), "benchmark": defang(event.figures.get("benchmark", ""))}
    f["top_model"] = defang(f["top_model"])
    f["unit"] = TEXT[lang]["units"].get(f["unit"], f["unit"])        # the figure names the unit; each language says it in its own word
    return sentence.format(**f)


NUMERIC = set("0123456789.,%")


def clip(text: str, limit: int) -> str:
    """`text` cut to `limit` characters with an ellipsis, and never inside a figure: a hook that stops at "64." states a
    number that is not on the board."""
    if len(text) <= limit:
        return text
    cut = limit - 1
    while cut > 0 and text[cut - 1] in NUMERIC and text[cut] in NUMERIC:
        cut -= 1
    return text[:cut].rstrip() + "…"


def tagged(event: Event, text: str, t: dict) -> str:
    """A community report is never written as fact: the unconfirmed tag goes on the very line that states it."""
    return text if event.verified else f"{t['unconfirmed_tag']} {text}"


def vendor_name(event: Event, rules: dict) -> str:
    return rules["vendor_names"].get(event.vendor, rules["vendor_names"]["other"])


def story_groups(events: list[Event], window: timedelta) -> list[list[Event]]:
    """Partition events into stories. A story is a connected set of events (rank.same_story, within `window` of each other
    pairwise-linked) whose members ALL lie within `window` of its newest member: a chain of links that each hold must not
    stretch one story across more than the window, or stale evidence would confirm a fresh recurrence. A chain that is too
    long is cut into slices anchored on the newest event, and each slice is regrouped on its own. Members keep input
    (rank) order; groups are ordered by their best-ranked member."""
    def linked(a, b):
        return same_story(a, b) and abs(a.when - b.when) <= window

    def components(members):
        parent = {id(m): id(m) for m in members}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for index, a in enumerate(members):
            for b in members[index + 1:]:
                if linked(a, b):
                    parent[find(id(b))] = find(id(a))
        found: dict[int, list[Event]] = {}
        for m in members:
            found.setdefault(find(id(m)), []).append(m)
        return list(found.values())

    groups: list[list[Event]] = []
    for component in components(events):
        if max(m.when for m in component) - min(m.when for m in component) <= window:
            groups.append(component)
            continue
        remaining = sorted(component, key=lambda m: m.when, reverse=True)
        while remaining:
            anchor = remaining[0].when
            sliced = [m for m in remaining if anchor - m.when <= window]
            remaining = [m for m in remaining if anchor - m.when > window]
            ranked = [m for m in component if any(m is s for s in sliced)]
            groups.extend(components(ranked))
    position = {id(e): n for n, e in enumerate(events)}
    return sorted(groups, key=lambda g: position[id(g[0])])


def select_with_absorbed(events: list[Event], rules: dict) -> tuple[list[Event], set[str]]:
    """The stories of an issue, best first, plus the ids of reports folded into them.

    Events that story_groups() puts together are one story: connected through rank.same_story, never stretched past
    the corroboration window, and independent of the order the events arrive in. Each group yields one canonical event (the
    first primary-source one, else the best ranked), the others are absorbed and their sources kept as evidence
    (`also_reported_by`).

    Only then are the slot limits applied, in the rank order of each group's best member: one source and kind may fill at
    most `max_per_source_kind` stories. A group that does not fit WAITS as its canonical event; its reports are absorbed
    into it, so a report is never published while its canonical event waits."""
    cfg = rules["export"]
    window = timedelta(hours=rules["scoring"]["corroboration_window_hours"])      # the same bound ranking uses
    limit = cfg["max_per_source_kind"]
    eligible, seen = [], set()
    for event in events:
        if event.score >= cfg["min_score"] and event.item_id not in seen:
            seen.add(event.item_id)
            eligible.append(event)

    picked: list[Event] = []
    absorbed: set[str] = set()
    for members in story_groups(eligible, window):
        newest = max(m.when for m in members)
        for m in members:                                 # ranking judged corroboration over the whole feed; a story that was cut out of a
            m.corroborated_by = corroborators(m, events, window, newest)      # longer chain keeps only the evidence that is still inside it
        canonical = next((m for m in members if m.trust == PRIMARY), members[0])
        for other in (m for m in members if m is not canonical):
            canonical.also_reported_by = list(dict.fromkeys(canonical.also_reported_by + other.also_reported_by + [other.source_name]))
            absorbed.add(other.item_id)
        if sum(1 for p in picked if (p.source, p.kind) == (canonical.source, canonical.kind)) < limit:
            picked.append(canonical)                  # otherwise it waits: not a story today, and not absorbed
    return picked[: cfg["max_stories"]], absorbed


def select(events: list[Event], rules: dict) -> list[Event]:
    return select_with_absorbed(events, rules)[0]


def build_bundle(events: list[Event], rules: dict, date: str) -> dict[str, str]:
    stories = select(events, rules)
    if not stories:
        raise NothingToPost(f"no event reached export.min_score={rules['export']['min_score']}")
    slug = f"ai-news-{date}"
    files = {f"tools/{slug}/brief.yaml": dump(_brief(stories, rules, slug, date, len(stories)))}
    for lang in LANGS:
        files[f"tools/{slug}/positioning/{lang}.yaml"] = dump({"lang": lang, **POSITIONING[lang]})
    posts = ["01-digest"] if len(stories) >= 2 else []
    spots = []
    for index, event in enumerate(stories[: rules["export"]["spotlights"]] if len(stories) >= 2 else stories, 1):
        slot = f"{len(posts) + 1:02d}-{event.kind}"
        while slot in posts:
            slot += "-b"
        posts.append(slot)
        spots.append((slot, index, event))
    for lang in LANGS:
        t = TEXT[lang]
        if len(stories) >= 2:
            files[f"campaigns/{slug}/posts/{lang}/01-digest.yaml"] = dump(_digest(stories, rules, lang, t, date))
        for slot, index, event in spots:
            files[f"campaigns/{slug}/posts/{lang}/{slot}.yaml"] = dump(_spotlight(slot, index, event, rules, lang, t, date))
    files[f"campaigns/{slug}/campaign.yaml"] = dump({
        "tool": slug, "langs": list(LANGS), "platforms": ["facebook"],
        "goal": "Share today's AI news from checked sources, each story with its source and how sure we are, and learn "
                "from the comments which topics readers want more of.",
        "links": {f"s{i}": e.url for i, e in enumerate(stories, 1)},
        "utm": {"medium": "social", "campaign": slug}, "schedule": []})
    for path, text in files.items():
        if "/posts/" in path and _post_has_link(text):
            raise ValueError(f"{path}: a post contains link-looking text; fix the template or the headline")
    return files


def _post_has_link(text: str) -> bool:
    data = yaml.safe_load(text)
    return any(has_link(s) for key in ("series", "slides", "platforms") for s in _strings(data.get(key)))


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _brief(stories, rules, slug, date, n) -> dict:
    features, claims = [], []
    for i, e in enumerate(stories, 1):
        fact = headline(e, "en")
        shown = fact if e.verified else f"[unconfirmed] {fact}"       # the brief is the studio's source material: say so here too
        features.append({"id": f"story-{i}-{e.kind}", "fact": f"{shown}. Source: {e.source_name}, {e.when.date().isoformat()}"})
        also = f"; also reported by {', '.join(e.also_reported_by)}" if e.also_reported_by else ""
        claims.append({"id": f"story-{i}", "text": shown, "evidence": f"{e.source_name}: {e.url}{also}",
                       "verified": e.verified})
    return {
        "slug": slug, "name": "AI News", "repo": "vohoailinh90/ai-news-radar", "status": "beta",
        "brand": {"emoji": "📰", "accent": "#0ea5e9"},
        "what_it_is": f"A daily digest of AI-model news for {date}, produced by ai-news-radar from the sources named in each claim. "
                      "Nothing here is tested by the radar; it reports what sources published.",
        "issue": {"date": date, "stories": n},
        "requirements": ["None: this is a news issue, nothing to install"],
        "features": features, "claims": claims,
        "limitations": ["The radar reports what its sources say and does not test any model or price itself",
                        "A story with verified: false is a community report that no official source has confirmed yet; "
                        "write it as reported, never as announced",
                        "Notable-feature picks from changelogs are chosen by keyword rules and need a human read"]
                       + (["A ranking story compares two rows of one public board as the board published them; the radar does not rerun the benchmark",
                           "A board's cost is its own measure for a run, not a price list, and a row is an agent, a model and a setting together, "
                           "not a model alone"] if any(e.kind == "ranking-board" for e in stories) else []),
        "story_to_confirm": [], "commercial_readiness": ["Not applicable: a news issue, nothing is sold"], "pricing_hypotheses": []}


def _cover_title(t, n, date):
    return t["digest_title"].format(n=n), t["eyebrow"].format(date=date)


def _digest(stories, rules, lang, t, date) -> dict:
    n = len(stories)
    title, eyebrow = _cover_title(t, n, date)
    items = [{"emoji": KIND_EMOJI.get(e.kind, "📰"), "title": tagged(e, f"{vendor_name(e, rules)} · {t['labels'].get(e.kind, e.kind)}", t),
              "text": tagged(e, headline(e, lang), t)} for e in stories]
    slides = [{"layout": "cover", "emoji": "📰", "eyebrow": eyebrow, "title": title, "subtitle": t["digest_subtitle"]},
              {"layout": "features", "title": title, "items": items}]
    unverified = [e for e in stories if not e.verified]
    if unverified:
        slides.append({"layout": "checklist", "tone": "bad", "title": t["unverified_title"],
                       "items": [tagged(e, headline(e, lang), t) for e in unverified][:6], "note": t["unverified_note"]})
    labels = "ABCD"
    slides.append({"layout": "poll", "eyebrow": eyebrow, "question": t["poll_q"], "hint": t["poll_hint"],
                   "options": [tagged(e, f"{vendor_name(e, rules)} · {t['labels'].get(e.kind, e.kind)}", t) for e in stories[:4]]})
    slides.append({"layout": "cta", "emoji": "🔔", "title": t["cta_title"], "text": t["cta_text"]})
    top = tagged(stories[0], headline(stories[0], lang), t)
    hook = t["hook"].format(n=n, date=date, top=top)
    if len(hook) > 125:
        room = 125 - len(t["hook"].format(n=n, date=date, top=""))
        hook = t["hook"].format(n=n, date=date, top=clip(top, max(room, 11)))
    lines = [f"{KIND_EMOJI.get(e.kind, '•')} {tagged(e, headline(e, lang), t)} — {e.source_name}" for e in stories]
    caption = hook + "\n\n" + "\n".join(lines) + "\n\n" + t["question"] + "\n"
    names = ", ".join(dict.fromkeys(e.source_name for e in stories))
    return _post("01-digest", "AI News", "s1", slides, caption, t["source_line"] + "\n" + t["other_sources"].format(names=names), t, rules)


def _spotlight(slot, index, event, rules, lang, t, date) -> dict:
    label, text = t["labels"].get(event.kind, event.kind), headline(event, lang)
    slides = [{"layout": "cover", "emoji": KIND_EMOJI.get(event.kind, "📰"),
               "eyebrow": tagged(event, f"{vendor_name(event, rules)} · {label}", t), "title": tagged(event, text, t),
               "subtitle": f"{event.source_name} · {event.when.date().isoformat()}"}]
    fig = event.figures
    if event.kind == "price-drop":
        slides.append({"layout": "stat", "eyebrow": tagged(event, label, t), "before": f"${fig['old_price']}", "after": f"${fig['new_price']}",
                       "label": t["per_million"], "caption": tagged(event, t["stat_caption"], t)})
    elif event.kind == "ranking-board":        # two different rows of one board, not one price that fell: no before/after (it is drawn struck through)
        slides.append({"layout": "stat", "eyebrow": tagged(event, label, t), "value": f"{fig['pct_cost']}%",
                       "label": t["ranking_label"].format(pct_score=fig["pct_score"]),
                       "caption": tagged(event, t["ranking_caption"].format(
                           model=defang(fig["model"]), value_cost=fig["value_cost"], top_model=defang(fig["top_model"]),
                           top_cost=fig["top_cost"], unit=t["units"].get(fig["unit"], fig["unit"])), t)})
    elif "score" in fig:
        slides.append({"layout": "stat", "eyebrow": tagged(event, label, t), "value": f"{fig['score']}%", "label": fig.get("benchmark", ""),
                       "caption": tagged(event, t["stat_caption"], t)})
    elif "percent" in fig:
        slides.append({"layout": "stat", "eyebrow": tagged(event, label, t), "value": f"{fig['percent']}%", "label": tagged(event, label, t),
                       "caption": tagged(event, t["stat_caption"], t)})
    facts = [{"emoji": "📎", "title": t["src"], "text": event.source_name},
             {"emoji": "📅", "title": t["when"], "text": event.when.date().isoformat()},
             {"emoji": "🔎", "title": t["trust"], "text": trust_label(event, t)}]
    if event.kind == "ranking-board":
        facts.append({"emoji": "ℹ️", "title": t["note"], "text": t["ranking_note"]})
    if event.title != text:
        facts.append({"emoji": "🔤", "title": t["original"], "text": tagged(event, event.title, t)})
    slides.append({"layout": "features", "title": tagged(event, label, t), "items": facts})
    if not event.verified:
        slides.append({"layout": "checklist", "tone": "bad", "title": t["unverified_title"], "items": [tagged(event, text, t)], "note": t["unverified_note"]})
    slides.append({"layout": "cta", "emoji": "🔔", "title": t["cta_title"], "text": t["cta_text"]})
    short = t["hooks"].get(event.kind)       # a headline too long for a hook has its own short line (its figures are in the brief)
    said = (short.format(label=label, model=defang(fig.get("model", "")), pct_score=fig.get("pct_score", ""), pct_cost=fig.get("pct_cost", ""))
            if short else t["spot_hook"].format(label=label, headline=text))
    hook = tagged(event, said, t)
    if len(hook) > 125:                       # Facebook folds a post after ~125 characters; the studio warns past it
        hook = clip(hook, 125)
    caption = hook + f"\n\n{tagged(event, text, t)}\n\n{trust_label(event, t)} · {event.source_name}\n\n{t['question']}\n"
    return _post(slot, "AI News", f"s{index}", slides, caption, t["source_one"], t, rules)


def _post(slot, series, link, slides, caption, source_line, t, rules) -> dict:
    return {"id": slot, "status": "draft", "format": "portrait", "series": series, "link": link, "slides": slides,
            "platforms": {"facebook": {"caption": caption, "first_comment": source_line + "\n", "hashtags": list(t["tags"])}},
            "notes": "Generated by ai-news-radar from checked sources. Read every line, confirm each source, then set status: ready."}


def post_status(path: Path) -> tuple[str, bool]:
    """(status, made_by_the_radar). An unreadable or odd file counts as 'unreadable': never a draft to overwrite."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return "unreadable", False
    if not isinstance(data, dict):
        return "unreadable", False
    if "status" not in data:                      # it never declared itself replaceable
        return "unreadable", False
    return str(data["status"]), str(data.get("notes", "")).startswith("Generated by ai-news-radar")


def plan_bundle(files: dict[str, str], dest: Path, *, force: bool = False) -> list[Path]:
    """Everything that could go wrong with writing, checked BEFORE anything is written. Returns the stale draft
    posts of the same issue that the new bundle no longer contains (deleted on write, drafts only)."""
    for parts in sorted({Path(p).parts[:2] for p in files}):
        existing = dest.joinpath(*parts)
        if existing.exists() and not force:
            raise WouldOverwrite(f"{existing} already exists; use --force to replace a draft issue")
    for path in files:
        target = dest / path
        if target.exists() and "/posts/" in path:
            status, _ = post_status(target)
            if status != "draft":
                raise WouldOverwrite(f"{target} has status '{status}'; the radar never overwrites an approved or posted post")
    stale = []
    for post_dir in {(dest / p).parent for p in files if "/posts/" in p}:
        for old in post_dir.glob("*.yaml") if post_dir.is_dir() else []:
            if old.relative_to(dest).as_posix() in files:
                continue
            status, ours = post_status(old)
            if status != "draft":
                raise WouldOverwrite(f"{old} is not a draft and is not part of the new issue; move it away first")
            if ours:                                    # a draft the owner wrote by hand is left alone
                stale.append(old)
    return stale


def write_bundle(files: dict[str, str], dest: Path, *, force: bool = False) -> list[Path]:
    """Write the bundle under `dest`. Never overwrites an existing issue unless `force`, never overwrites a post
    that is no longer a draft, and removes the issue's own stale drafts (a post the new bundle replaced)."""
    stale = plan_bundle(files, dest, force=force)
    for old in stale:
        old.unlink()
    written = []
    for path, text in files.items():
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(target)
    return written


def render_report(events: list[Event], reports: list, rules: dict, now: datetime) -> str:
    lines = [f"# Radar report {now.date().isoformat()}", "", "## Sources", "", "| source | ok | items | note |", "|---|---|---|---|"]
    lines += [f"| {r.id} | {'yes' if r.ok else 'NO'} | {r.items} | {r.note} |" for r in reports]
    lines += ["", "## Ranked events", "", "| score | kind | vendor | verified | source | headline |", "|---|---|---|---|---|---|"]
    lines += [f"| {e.score:.1f} | {e.kind} | {e.vendor} | {'yes' if e.verified else 'no'} | {e.source_name} | {e.title[:110]} |" for e in events]
    return "\n".join(lines) + "\n"
