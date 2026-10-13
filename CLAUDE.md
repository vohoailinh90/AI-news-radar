# AI News Radar

Scans AI-model sources and turns what is worth posting into a bundle for `vohoailinh90/tool-marketing-studio`.
Read `README.md` first; it is the contract.

## Rules that must not be broken

- **Never fabricate news.** Every story traces to a source in `config/sources.yaml`. No demo or placeholder stories
  in `out/` or `data/`; fixtures live in `tests/fixtures` and say whether they are real or `synthetic_*`.
- **Verified means a primary source.** Community sources (Hacker News) are `verified: false` until a primary source
  corroborates them (`Event.verified`). Posts must never state an unconfirmed report as fact.
- **Rules match actions, not nouns** (`config/rules.yaml`). Add a negative test with every new rule.
- **Posts are drafts.** `status: draft` always; the exporter never overwrites an issue (`--force` only replaces drafts)
  and never overwrites a post whose status is not `draft`.
- **No link-looking text in posts** (`radar/textsafe.py`). Links go in `campaign.yaml` `links:` and `{link}` in the first comment.
- **Numbers come from `Event.figures`**, so the brief and all three languages state identical figures.
- A dead source is reported, never fatal; if every source fails, `data/state.json` is left untouched.
- This repo does not modify `tool-marketing-studio`. `verify` runs the studio's own `check` on a throwaway copy.

## Commands

`python radar.py scan|export|run|verify`, `python -m pytest -q`. A computable check belongs in a test, not in a review.
When you ship a fix, add the test that would have caught it, and break the behavior once to prove the test notices.
