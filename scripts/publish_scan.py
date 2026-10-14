#!/usr/bin/env python3
"""Deliver the daily scan through a pull request instead of a push to the default branch.

The default branch is protected (pull request, required check, no bypass), so the scan Action cannot push
to it. It works on the branch `scan-data` and keeps one pull request open from it:

  prepare   start the work tree from the default branch; when the branch holds a scan that is still waiting
            (its latest pull request is open, never opened, or did not cover the branch tip: a scan pushed after
            the previous pull request was finished) bring the branch's own changes over it
            (`git merge --squash`: what the branch added to the default branch it was built on, nothing else),
            so the scan continues from yesterday's state instead of re-detecting yesterday's news. A conflict
            with the default branch stops the run: guessing would corrupt the state file
  (scan)    `python radar.py run`, unchanged
  publish   commit `data/` and `out/`, push the branch, open the pull request if none is open, and dispatch
            `ci.yml` on the branch whenever its tip has no run of the required check yet (so a refused
            dispatch is retried by the next run, even when nothing else changed)

Why the branch is rebuilt on the default branch every day, never merged with it: a token the workflow gets
may not push a commit that changes `.github/workflows/`, and a merge of the default branch into the
branch would carry exactly such changes. A rebuilt branch is the default branch plus one commit that only
touches `data/` and `out/`. It is force-pushed with a lease on the tip prepare saw.

Why `ci.yml` is dispatched: commits and pull requests made with the workflow token do not trigger workflows,
so a required check would never report. `workflow_dispatch` is the one event the token may start.

Standard library only: it runs on any runner, with or without the `gh` CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BRANCH, BASE, CI, CHECK = "scan-data", "main", "ci.yml", "tests"
PATHS = ("data", "out")
API = "https://api.github.com"
IDENTITY = ("-c", "user.name=ai-news-radar", "-c", "user.email=ai-news-radar@users.noreply.github.com")
TITLE = "Daily scan data"
BODY = ("Daily scan of the AI news sources, committed by the scan workflow.\n\n"
        "- `data/` is the radar's state and `out/` the studio-ready issues (every post is a draft).\n"
        "- Merge this pull request to publish them. The next scan then starts from a fresh branch.\n"
        "- The branch is rebuilt on the default branch every day and force-pushed: do not commit to it.\n"
        "- Closing this pull request without merging drops what the branch holds; the next scan starts over.")


class PublishError(RuntimeError):
    pass


class GitHubError(PublishError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


class GitHub:
    """The three REST calls the workflow needs. Replaced by a fake in tests."""

    def __init__(self, repo: str, token: str, opener=urllib.request.urlopen):
        self.repo, self.token, self.opener = repo, token, opener
        self.owner = repo.split("/")[0]

    def _call(self, method: str, path: str, body: dict | None = None):
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ai-news-radar"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(API + path, method=method, headers=headers,
                                         data=None if body is None else json.dumps(body).encode("utf-8"))
        try:
            with self.opener(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("message", detail)
            except (ValueError, AttributeError):
                pass
            raise GitHubError(exc.code, str(detail)[:300]) from exc
        except urllib.error.URLError as exc:
            raise GitHubError(0, str(exc.reason)) from exc
        return json.loads(raw) if raw else None

    def latest_pull_request(self, head: str) -> dict | None:
        """The newest pull request (any state) from branch `head`, or None."""
        query = urllib.parse.urlencode({"head": f"{self.owner}:{head}", "state": "all", "sort": "created", "direction": "desc", "per_page": 1})
        found = self._call("GET", f"/repos/{self.repo}/pulls?{query}")
        return found[0] if found else None

    def has_check_run(self, sha: str, name: str) -> bool:
        """Whether commit `sha` already has a check run called `name` (queued, running or finished)."""
        query = urllib.parse.urlencode({"check_name": name, "per_page": 1})
        found = self._call("GET", f"/repos/{self.repo}/commits/{sha}/check-runs?{query}")
        return bool(found and found.get("total_count", 0) > 0)

    def create_pull_request(self, head: str, base: str, title: str, body: str) -> dict:
        return self._call("POST", f"/repos/{self.repo}/pulls", {"head": head, "base": base, "title": title, "body": body})

    def dispatch(self, workflow: str, ref: str) -> None:
        self._call("POST", f"/repos/{self.repo}/actions/workflows/{workflow}/dispatches", {"ref": ref})


def git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    done = subprocess.run(["git", *IDENTITY, *args], cwd=cwd, capture_output=True, text=True)
    if check and done.returncode != 0:
        raise PublishError(f"git {' '.join(args)} failed: {(done.stderr or done.stdout).strip()}")
    return done


def rev(name: str, cwd: Path) -> str | None:
    done = git("rev-parse", "--verify", "--quiet", name, cwd=cwd, check=False)
    return done.stdout.strip() if done.returncode == 0 else None


def differs(*args: str, cwd: Path) -> bool:
    """`git diff --quiet`: exit 1 means a difference, 0 none; anything else is an error, never an answer."""
    done = git("diff", "--quiet", *args, cwd=cwd, check=False)
    if done.returncode not in (0, 1):
        raise PublishError(f"git diff {' '.join(args)} failed: {done.stderr.strip()}")
    return done.returncode == 1


def prepare(gh, cwd: Path, branch: str = BRANCH, base: str = BASE) -> str:
    """Leave the work tree on `branch`, based on the default branch. Returns "continue" when a waiting scan was laid
    over it, "fresh" otherwise."""
    git("fetch", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}", cwd=cwd)
    listed = git("ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}", cwd=cwd, check=False)      # exact: `foo/{branch}` is another branch
    if listed.returncode not in (0, 2):                       # 2 = no such branch; anything else is a real failure
        raise PublishError(f"cannot list the branches of origin: {listed.stderr.strip()}")
    carry = False
    if listed.returncode == 0:
        git("fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", cwd=cwd)
        latest = gh.latest_pull_request(branch)
        # A finished pull request settles only the tip it covered. A scan pushed after it (its own pull request failed to
        # open) is still waiting, and so is any tip the pull request cannot be shown to have covered: never discard on doubt.
        carry = (latest is None or latest.get("state") == "open"
                 or (latest.get("head") or {}).get("sha") != rev(f"refs/remotes/origin/{branch}", cwd))
    else:
        git("update-ref", "-d", f"refs/remotes/origin/{branch}", cwd=cwd, check=False)      # no stale expectation for the lease
    git("checkout", "-B", branch, f"origin/{base}", cwd=cwd)
    if carry:
        merged = git("merge", "--squash", f"origin/{branch}", cwd=cwd, check=False)
        if merged.returncode != 0:
            git("reset", "--hard", "HEAD", cwd=cwd)
            raise PublishError(f"the waiting scan on {branch} conflicts with {base} ({(merged.stdout + merged.stderr).strip()[:300]}); "
                               f"fix it on the branch, or delete the branch to start over")
    return "continue" if carry else "fresh"


def reported(gh, sha: str, check: str) -> bool:
    """Whether the required check already has a run on `sha`. A failed lookup counts as "no": one extra CI run costs
    less than a pull request whose required check never reports."""
    try:
        return gh.has_check_run(sha, check)
    except GitHubError as exc:
        print(f"could not look up the {check} check on {sha[:10]} ({exc}); dispatching to be safe")
        return False


def publish(gh, cwd: Path, branch: str = BRANCH, base: str = BASE, workflow: str = CI, today: str | None = None,
            check: str = CHECK) -> str:
    """Commit, push, open the pull request, dispatch CI. Returns "nothing" or "published". A failure after the push
    raises, and leaves the data safe on the branch: the next run retries the pull request."""
    today = today or datetime.now(timezone.utc).date().isoformat()
    git("add", "-A", "--", *[p for p in PATHS if (cwd / p).exists()], cwd=cwd)
    if differs("--cached", cwd=cwd):
        git("commit", "-m", f"scan {today}", cwd=cwd)
    if not differs(f"origin/{base}", "HEAD", cwd=cwd):
        print(f"nothing to deliver: {branch} would not differ from {base}")
        return "nothing"
    remote = rev(f"refs/remotes/origin/{branch}", cwd)
    unchanged = (remote is not None and rev(f"{remote}^{{tree}}", cwd) == rev("HEAD^{tree}", cwd)
                 and rev(f"{remote}~1", cwd) == rev("HEAD~1", cwd))
    if not unchanged:
        lease = [f"--force-with-lease=refs/heads/{branch}:{remote}"] if remote else []
        git("push", *lease, "origin", f"HEAD:refs/heads/{branch}", cwd=cwd)
        print(f"pushed {branch}")
    latest = gh.latest_pull_request(branch)
    created = latest is None or latest.get("state") != "open"
    if created:
        try:
            gh.create_pull_request(branch, base, TITLE, BODY)
        except GitHubError as exc:
            raise PublishError(
                f"the scan is on branch {branch}, but the pull request could not be opened ({exc}). Turn on Settings > Actions > "
                "General > Workflow permissions > 'Allow GitHub Actions to create and approve pull requests'; the next run opens it.") from exc
        print(f"opened a pull request from {branch}")
    tip = remote if unchanged else rev("HEAD", cwd)
    if created or not unchanged or not reported(gh, tip, check):      # a new head, a new pull request, or a head the required check never ran on
        try:
            gh.dispatch(workflow, branch)
        except GitHubError as exc:
            raise PublishError(f"the pull request is open, but {workflow} could not be dispatched on {branch} ({exc}); "
                               "run it by hand (Actions > CI > Run workflow) or the required check will not report") from exc
        print(f"dispatched {workflow} on {branch}")
    return "published"


def main(argv: list[str] | None = None, env=None, cwd: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("step", choices=("prepare", "publish"))
    parser.add_argument("--branch", default=BRANCH)
    parser.add_argument("--base", default=BASE)
    parser.add_argument("--ci-workflow", default=CI)
    parser.add_argument("--check", default=CHECK, help="name of the required check the workflow dispatch must produce")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    try:
        repo, token = env.get("GITHUB_REPOSITORY"), env.get("GITHUB_TOKEN")
        if not repo or not token:
            raise PublishError("GITHUB_REPOSITORY and GITHUB_TOKEN must be set (the workflow sets both)")
        gh, where = GitHub(repo, token), cwd or Path.cwd()
        if args.step == "prepare":
            print(f"{args.branch}: {prepare(gh, where, args.branch, args.base)}")
        else:
            publish(gh, where, args.branch, args.base, args.ci_workflow, check=args.check)
    except PublishError as exc:
        print(f"::error::{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
