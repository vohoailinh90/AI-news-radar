"""A feed that reaches us through a third party is only as trustworthy as that party. For a source marked
`confirm_page: true`, an event stays `primary` only when the vendor's own page, fetched by the radar itself,
carries the same headline. A page that cannot be fetched, or says something else, demotes the event to
`community`: it is still reported, but labelled unconfirmed everywhere."""

from __future__ import annotations

import html
import re

from .http import FetchError
from .models import COMMUNITY, PRIMARY, Event
from .textsafe import normalize

MAX_PAGE_BYTES = 2_000_000
MAX_CONFIRMATIONS = 15          # a scan fetches at most this many pages
OG_TITLE = re.compile(r"""<meta[^>]+property=["']og:title["'][^>]*content=["']([^"']+)|<meta[^>]+content=["']([^"']+)["'][^>]*property=["']og:title""", re.I)
TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def squash(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", normalize(html.unescape(text)).casefold()).split())


COMMENT = re.compile(r"<!--.*?-->", re.S)
SEPARATOR = re.compile(r"\s+[\\|\u2013\u2014\u00b7-]\s+")


def page_titles(page: str) -> list[str]:
    """The headline(s) a page declares about itself: og:title first, then the <title> element.
    Comments are dropped first, so a tag mentioned inside one cannot stand in for the real one."""
    page = COMMENT.sub("", page)
    found = []
    og = OG_TITLE.search(page)
    if og:
        found.append(og.group(1) or og.group(2))
    title = TITLE.search(page)
    accepted = [squash(t) for t in found if t and t.strip()]
    if title and title.group(1).strip():
        raw = html.unescape(title.group(1))
        parts = SEPARATOR.split(raw)          # "Headline \\ Site name": the headline alone, or everything before a separator
        accepted += [squash(" ".join(parts[:i])) for i in range(1, len(parts) + 1)]
    return accepted


def confirms(page: str, headline: str) -> bool:
    wanted = squash(headline)
    if not wanted:
        return False
    return wanted in page_titles(page)          # equality only: a shorter, invented headline is not confirmed by a longer real one


def _page_titles(url: str, fetch, cache: dict):
    """What the page at `url` says about itself (squashed titles), or None when it cannot be read. Fetched once per URL per
    scan, at most MAX_CONFIRMATIONS URLs. The cache keeps the page's titles, never one headline's verdict: every event
    is judged against them separately."""
    if url not in cache:
        if len(cache) >= MAX_CONFIRMATIONS:
            cache[url] = None                           # over the cap: unconfirmed, never silently trusted
        else:
            try:
                cache[url] = set(page_titles(fetch(url, max_bytes=MAX_PAGE_BYTES).decode("utf-8", "replace")))
            except FetchError:
                cache[url] = None
    return cache[url]


def _checks_out(event: Event, fetch, cache: dict) -> bool:
    titles = _page_titles(event.url, fetch, cache)
    return titles is not None and squash(event.title) in titles


def confirm_events(events: list[Event], flagged: set[str], fetch, cache: dict | None = None) -> list[str]:
    """Demote to community every flagged-source event the vendor's page does not confirm. Returns notes for the run report."""
    notes, cache = [], {} if cache is None else cache
    for event in events:
        if event.source not in flagged or event.trust != PRIMARY or not event.url.startswith("https://"):
            continue
        if not _checks_out(event, fetch, cache):
            event.trust = COMMUNITY
            notes.append(f"{event.source}: the page for '{event.title[:60]}' did not confirm the headline; reported as unconfirmed")
    return notes


def reconfirm_events(events: list[Event], original: dict[str, str], fetch, cache: dict | None = None) -> None:
    """Events kept pending after a failed confirmation get another chance each scan: a page that is reachable now and
    carries the headline restores the source's own trust. Events that were never demoted are not fetched again."""
    cache = {} if cache is None else cache
    for event in events:
        trust = original.get(event.source)
        if trust is None or event.trust == trust or not event.url.startswith("https://"):
            continue
        if _checks_out(event, fetch, cache):
            event.trust = trust
