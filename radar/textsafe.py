"""Text that reaches a post must not contain anything the studio would read as a link
(`studio.py check` rejects any URL, www. or bare domain). Same root-zone TLD list as the studio,
vendored in tlds.txt, applied to NFKC-normalised text."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

TLDS = sorted({w for line in Path(__file__).with_name("tlds.txt").read_text(encoding="utf-8").splitlines()
               if not line.startswith("#") for w in line.split()}, key=len, reverse=True)
DOMAIN = re.compile(r"(?i)(?<![a-z0-9-])((?:[a-z0-9][a-z0-9-]{0,62}\.){1,10})(?:" + "|".join(map(re.escape, TLDS))
                    + r")(?![a-z0-9À-ɏ̀-ͯḀ-ỿ-])")
SCHEME = re.compile(r"(?i)\bh[tx]{2}ps?\s*:\s*//\S*|\bwww\.\S*")
MAX_LEN = 400


def has_link(text: str) -> bool:
    text = unicodedata.normalize("NFKC", text)
    return bool(SCHEME.search(text) or DOMAIN.search(text))


def defang(text: str) -> str:
    """Remove link-looking tokens: `claude.ai` -> `claude`, a full URL -> nothing. Also strips
    Markdown backticks and collapses whitespace, and caps the length."""
    text = unicodedata.normalize("NFKC", text).replace("`", "")
    text = SCHEME.sub("", text)
    text = DOMAIN.sub(lambda m: m.group(1).rstrip("."), text)
    text = " ".join(text.split())
    return text if len(text) <= MAX_LEN else text[: MAX_LEN - 1].rstrip() + "…"
