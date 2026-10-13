"""A Markdown changelog with `## <version>` headings and `- ` bullets (no dates).
State remembers the newest version seen, so only newer versions are reported."""

from __future__ import annotations

import re

from ..config import Source
from ..models import Item
from . import Context, SourceError

HEADING = re.compile(r"^##\s+v?(\d+(?:\.\d+)*)\s*$")


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def parse(text: str) -> list[tuple[str, list[str]]]:
    """[(version, bullets)] in file order (newest first in a Keep-a-Changelog style file)."""
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        match = HEADING.match(line)
        if match:
            sections.append((match.group(1), []))
        elif sections and line.startswith("- "):
            sections[-1][1].append(line[2:].strip())
        elif sections and sections[-1][1] and line.startswith("  ") and line.strip():
            sections[-1][1][-1] += " " + line.strip()      # a wrapped bullet
    return sections


def collect(source: Source, ctx: Context):
    sections = parse(ctx.fetch(source.url, max_bytes=source.opt("max_bytes", 3_000_000)).decode("utf-8", "replace"))
    if not sections:
        raise SourceError(f"{source.id}: no '## <version>' headings found; the file format changed")
    known = ctx.state.versions.get(source.id)
    ordered = sorted(sections, key=lambda s: version_key(s[0]), reverse=True)
    if known:
        fresh = [s for s in ordered if version_key(s[0]) > version_key(known)]
    else:
        fresh = ordered[: int(source.opt("first_run_versions", 1))]
    ctx.state.versions[source.id] = ordered[0][0]
    items = [Item(id=f"{source.id}:{version}", source=source.id, title=f"{source.name} {version}",
                  url=source.url.replace("raw.githubusercontent.com", "github.com", 1).replace("/main/", "/blob/main/", 1),
                  body="\n".join(bullets), extra={"version": version, "bullets": bullets})
             for version, bullets in fresh]
    return items, []
