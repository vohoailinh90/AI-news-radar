# AI News Radar

Scans AI-model sources and turns what is worth posting into a bundle for `vohoailinh90/tool-marketing-studio`.
Read `README.md` first; it is the contract.

## Rules that must not be broken

- **Never fabricate news.** Every story traces to a source in `config/sources.yaml`. No demo or placeholder stories
  in `out/` or `data/`; fixtures live in `tests/fixtures` and say whether they are real or `synthetic_*`.
- **Verified means a primary source.** Community sources (Hacker News) are `verified: false` until a primary source
  corroborates them (`Event.verified`). A source read through a third party (`confirm_page`) is primary only for
  the events whose vendor page the radar fetched and found to carry the same headline. Posts must never state an
  unconfirmed report as fact, and every line that restates one carries the unconfirmed tag.
- **Rules match actions, not nouns** (`config/rules.yaml`). Add a negative test with every new rule.
- **Posts are drafts.** `status: draft` always; the exporter never overwrites an issue (`--force` only replaces drafts)
  and never overwrites a post whose status is not `draft`.
- **No link-looking text in posts** (`radar/textsafe.py`). Links go in `campaign.yaml` `links:` and `{link}` in the first comment.
- **Numbers come from `Event.figures`**, so the brief and all three languages state identical figures.
- A dead source is reported, never fatal; if every source fails, `data/state.json` is left untouched.
- The daily Action never pushes to the default branch: `scripts/publish_scan.py` keeps the branch `scan-data` and one pull request open
  from it (`README.md`, "Action hằng ngày"). The branch is rebuilt on the default branch and touches only `data/` and `out/`, because the
  workflow token may not push changes to `.github/workflows/`. Keep `ci.yml` dispatchable and its job named `tests`.
- This repo does not modify `tool-marketing-studio`. `verify` runs the studio's own `check` on a throwaway copy.

## Commands

`python radar.py scan|export|run|verify`, `python -m pytest -q`, `python scripts/publish_scan.py prepare|publish` (the Action only). A computable check belongs in a test, not in a review.
When you ship a fix, add the test that would have caught it, and break the behavior once to prove the test notices.

<!-- team-onboarding:start -->
## Team agent process

`vohoailinh90/AI-news-radar` is registered with the free-model team: `team` (Claude leads) and `team-codex` (Codex leads), with free models writing the tests, the code and a pre-review. The details are in `docs/TEAM.md`; the settings that differ per repository live in `repos.conf` on the VPS, not here.

- Base branch `main`; sensitivity `public`; gates setup `python3 -m venv --system-site-packages .venv && .venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt`; suggested test commands `.venv/bin/python -m pytest -q`.
- If you are a team worker (tester, coder or reviewer), follow the handoff files you are given and touch only the files the task names. Never edit CI (`.github/`), agent instructions (`CLAUDE.md`, `AGENTS.md`, `.claude/`, `.codex/`), lockfiles or `repos.conf` unless the task says so.

If you are Claude Code or Codex working on this repository directly (a spec, an audit or an ordinary change), this is the pull-request process:

- When your work here is ready to ship, open the pull request yourself; do not wait to be asked, unless the user asked for a local change only or this repository's own rules say a pull request is opened only on request.
- As soon as it is open, ask Codex for a review with one comment on the PR: `@codex review` (add `@codex security review` where this repository's rules, or the change itself, call for one: authentication, authorization, secrets, payments, personal data). Codex never reviews on its own here. Ask once per head commit for each review command, never while a request is unanswered, and never mention Codex in the PR description or a commit message. Where the repository has `scripts/codex_round_count.py`, run it before each request.
- Check each finding against the code. Confirmed: fix it with a regression test, push, and ask again on the new head. Wrong: answer on its thread with the evidence. There is no cap on rounds; a rejected P2/P3 stands and blocks nothing; a P0/P1 you cannot settle goes to the repository owner and the PR is not merged.
- Merge it yourself, without waiting for approval, once all of these hold on the current head: every review you requested of that exact commit has no finding left (a bare thumbs-up is not a review, unless this repository's own rules accept one for that exact request), CI is green, there is no merge conflict, and the base branch's protection (or a merge queue) requires branches to be up to date and applies to admins. Immediately before merging, re-read the PR head and the base head: if the PR head moved, review it again; if the base moved, merge it in and re-validate. Merge scoped to the validated head SHA. If that protection is missing, do not merge: leave the PR open, say it is clean and why it was not merged, and tell the owner.
- If Codex gives no sign of life within 10 minutes, retry once per head; an explicit error or usage-limit reply needs no wait before that one retry. If it acknowledges but never delivers a review, or its task fails, do not retry: the PR is BLOCKED. The PR is also BLOCKED when the retry gets no answer. A BLOCKED PR stays open and is reported once; never merge it unreviewed, and never use another route (API, CLI, Action) to get a review.

Where this repository documents its own rule for one of these steps elsewhere (who merges, what gates a merge, how the review is requested, a cap on rounds), that rule governs that step; this process covers whatever the repository leaves unsaid.
<!-- team-onboarding:end -->
