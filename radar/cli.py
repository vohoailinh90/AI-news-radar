"""python radar.py scan | export | run | verify | report"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from . import detect as detect_mod
from . import export as export_mod
from . import rank
from .config import ROOT, ConfigError, load_rules, load_sources
from .http import FetchError, fetch
from .models import SourceReport, event_from_dict, event_to_dict
from .sources import Context, SourceError, changelog, hn, leaderboard, openrouter, rss
from .store import State, utcnow

COLLECTORS = {"rss": rss, "changelog_md": changelog, "yaml_leaderboard": leaderboard, "openrouter": openrouter, "hn": hn}


def scan(root: Path, fetcher=fetch, now=None) -> tuple[list, list[SourceReport]]:
    """Run every enabled source. A dead source is recorded and skipped; it never fails the scan."""
    now = now or utcnow()
    sources, rules = load_sources(root), load_rules(root)
    state = State.load(root / "data" / "state.json")
    ctx = Context(fetch=fetcher, state=state, now=now, rules=rules)
    items, events, reports = [], [], []
    for source in sources:
        if not source.enabled:
            continue
        try:
            got_items, got_events = COLLECTORS[source.kind].collect(source, ctx)
        except (FetchError, SourceError) as exc:
            reports.append(SourceReport(source.id, False, 0, str(exc)[:200]))
            continue
        except Exception as exc:                      # a parser bug in one source must not hide the others
            reports.append(SourceReport(source.id, False, 0, f"internal error: {type(exc).__name__}: {exc}"[:200]))
            continue
        note = ""
        if source.kind == "yaml_leaderboard":
            newest = (state.snapshots.get(source.id) or {}).get("newest")
            if newest:
                age = (now.date() - datetime_date(newest)).days
                note = f"newest entry {newest}" + (f" — STALE ({age} days old)" if age > source.opt("stale_days", 90) else "")
        reports.append(SourceReport(source.id, True, len(got_items) + len(got_events), note))
        items += got_items
        events += got_events
    by_id = {s.id: s for s in sources}
    events += detect_mod.detect(items, by_id, rules, now)
    fresh = []
    for event in events:
        if event.item_id and event.item_id in state.seen:
            continue
        state.mark_seen(event.item_id, now) if event.item_id else None
        fresh.append(event)
    ranked = rank.score_events(fresh, rules, now, {i.id: i.points for i in items})
    if any(r.ok for r in reports):                    # all sources down: keep the old state, do not lose a baseline
        state.save(now)
    return ranked, reports


def datetime_date(text: str):
    from datetime import date
    return date.fromisoformat(text)


def save_scan(root: Path, events, reports, now) -> Path:
    path = root / "data" / "last-scan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"generated": now.isoformat(), "events": [event_to_dict(e) for e in events],
                                "sources": [asdict(r) for r in reports]}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def load_scan(root: Path):
    path = root / "data" / "last-scan.json"
    if not path.exists():
        raise SystemExit("no scan yet: run `python radar.py scan` first")
    data = json.loads(path.read_text(encoding="utf-8"))
    return [event_from_dict(e) for e in data["events"]], [SourceReport(**s) for s in data["sources"]], data["generated"]


def cmd_scan(args, root: Path) -> int:
    now = utcnow()
    events, reports = scan(root, now=now)
    save_scan(root, events, reports, now)
    for r in reports:
        print(f"{'ok  ' if r.ok else 'FAIL'} {r.id}: {r.items} item(s) {r.note}".rstrip())
    print(f"{len(events)} new event(s); top: " + "; ".join(f"{e.score:.1f} {e.kind}" for e in events[:3]))
    if not any(r.ok for r in reports):
        print("every source failed; state was left untouched", file=sys.stderr)
        return 2
    return 0


def cmd_export(args, root: Path) -> int:
    events, reports, generated = load_scan(root)
    rules = load_rules(root)
    date = args.date or generated[:10]
    try:
        files = export_mod.build_bundle(events, rules, date)
    except export_mod.NothingToPost as exc:
        print(f"nothing to post: {exc}")
        return 3
    slug = f"ai-news-{date}"
    out = root / "out" / slug
    try:
        written = export_mod.write_bundle(files, out, force=args.force)
        if args.studio:
            written += export_mod.write_bundle(files, Path(args.studio), force=args.force)
    except export_mod.WouldOverwrite as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 4
    (out / "REPORT.md").write_text(export_mod.render_report(events, reports, rules, utcnow()), encoding="utf-8")
    print(f"{len(written)} file(s) for {slug} -> {out}" + (f" and {args.studio}" if args.studio else ""))
    print(f"next, in the studio: python studio.py check {slug}")
    return 0


def cmd_verify(args, root: Path) -> int:
    """Run the studio's own `check` on an exported issue, in a throwaway copy of its engine."""
    studio, out = Path(args.studio), root / "out" / args.slug
    if not out.is_dir():
        print(f"no bundle at {out}: run export first", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        shutil.copytree(studio / "mkt", work / "mkt")
        for name in ("studio.py", "studio.yaml"):
            if (studio / name).exists():
                shutil.copy2(studio / name, work / name)
        shutil.copytree(out, work, dirs_exist_ok=True)
        result = subprocess.run([sys.executable, "studio.py", "check", args.slug], cwd=work, text=True, capture_output=True)
    print(result.stdout + result.stderr, end="")
    return result.returncode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="radar.py", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="fetch every source and save the ranked events")
    ex = sub.add_parser("export", help="turn the last scan into a studio bundle under out/")
    ex.add_argument("--date", help="issue date, YYYY-MM-DD (default: the scan's date)")
    ex.add_argument("--studio", help="also write the bundle into this tool-marketing-studio checkout")
    ex.add_argument("--force", action="store_true", help="replace an existing draft issue")
    run = sub.add_parser("run", help="scan, then export")
    run.add_argument("--studio")
    run.add_argument("--force", action="store_true")
    run.add_argument("--date")
    ver = sub.add_parser("verify", help="run the studio's `check` on an exported issue")
    ver.add_argument("--studio", required=True)
    ver.add_argument("--slug", required=True)
    args = parser.parse_args(argv)
    root = ROOT
    try:
        if args.cmd == "scan":
            return cmd_scan(args, root)
        if args.cmd == "export":
            return cmd_export(args, root)
        if args.cmd == "run":
            code = cmd_scan(args, root)
            return code if code else cmd_export(args, root)
        return cmd_verify(args, root)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
