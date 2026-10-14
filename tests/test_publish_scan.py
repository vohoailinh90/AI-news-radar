"""The scan Action delivers through a pull request (scripts/publish_scan.py): real git against a bare remote, a fake GitHub."""

import argparse
import io
import json
import subprocess
import urllib.error
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from radar import cli
from radar.config import ROOT
from radar.models import Event
from tests.helpers import NOW, REAL, fetcher_for, make_root
from scripts import publish_scan as ps


def sh(*args, cwd, check=True):
    done = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and done.returncode != 0:
        raise AssertionError(f"{args}: {done.stderr or done.stdout}")
    return done.stdout.strip()


class FakeGitHub:
    def __init__(self, resolve, latest=None, refuse_create=False, refuse_dispatch=False, refuse_lookup=False):
        self.resolve, self.latest = resolve, latest
        self.refuse_create, self.refuse_dispatch, self.refuse_lookup = refuse_create, refuse_dispatch, refuse_lookup
        self.created, self.dispatched, self.reported = [], [], set()

    def has_check_run(self, sha, name):
        if self.refuse_lookup:
            raise ps.GitHubError(403, "Resource not accessible by integration")
        return sha in self.reported

    def latest_pull_request(self, head):
        return self.latest

    def create_pull_request(self, head, base, title, body):
        if self.refuse_create:
            raise ps.GitHubError(403, "GitHub Actions is not permitted to create or approve pull requests.")
        self.created.append((head, base, title))
        self.latest = {"state": "open"}

    def dispatch(self, workflow, ref):
        if self.refuse_dispatch:
            raise ps.GitHubError(403, "Resource not accessible by integration")
        self.dispatched.append((workflow, ref))
        self.reported.add(self.resolve(ref))                                                     # a dispatched run reports on the tip it was dispatched on


class World:
    """A bare `origin` holding main, the runner's clone `work`, and a second clone `owner` that plays the repository owner."""

    def __init__(self, tmp_path, files=("data/.gitkeep", "out/.gitkeep", "radar.py", ".github/workflows/ci.yml")):
        self.origin, self.work, self.owner = tmp_path / "origin.git", tmp_path / "work", tmp_path / "owner"
        sh("git", "init", "--bare", "-b", "main", str(self.origin), cwd=tmp_path)
        seed = tmp_path / "seed"
        seed.mkdir()
        sh("git", "init", "-b", "main", cwd=seed)
        for path in files:
            (seed / path).parent.mkdir(parents=True, exist_ok=True)
            (seed / path).write_text("v1\n")
        self.commit(seed, "seed")
        sh("git", "remote", "add", "origin", str(self.origin), cwd=seed)
        sh("git", "push", "origin", "main", cwd=seed)
        for clone in (self.work, self.owner):
            sh("git", "clone", str(self.origin), str(clone), cwd=tmp_path)
            for key, value in (("user.name", "t"), ("user.email", "t@example.org")):
                sh("git", "config", key, value, cwd=clone)

    def gh(self, **kwargs):
        return FakeGitHub(lambda ref: self.remote("rev-parse", ref), **kwargs)

    @staticmethod
    def commit(cwd, message):
        for key, value in (("user.name", "t"), ("user.email", "t@example.org")):
            sh("git", "config", key, value, cwd=cwd)
        sh("git", "add", "-A", cwd=cwd)
        sh("git", "commit", "-m", message, cwd=cwd)

    def day(self, n):
        """What `python radar.py run` leaves behind for day n: the state, and that day's issue."""
        (self.work / "data" / "state.json").write_text(json.dumps({"day": n}))
        issue = self.work / "out" / f"ai-news-2026-10-0{n}"
        issue.mkdir(parents=True, exist_ok=True)
        (issue / "post.yaml").write_text(f"status: draft\nday: {n}\n")

    def run(self, gh, n, today=None):
        mode = ps.prepare(gh, self.work)
        if n:
            self.day(n)
        return mode, ps.publish(gh, self.work, today=today or f"2026-10-0{n or 9}")

    def remote(self, *args):
        return sh("git", *args, cwd=self.origin, check=True)

    def branch_files(self):
        return sorted(self.remote("diff", "--name-only", "main", "scan-data").split())

    def owner_merges(self, how):
        sh("git", "pull", "-q", "origin", "main", cwd=self.owner)
        sh("git", "fetch", "-q", "origin", "scan-data", cwd=self.owner)
        if how == "merge":
            sh("git", "merge", "--no-ff", "-m", "merge pr", "origin/scan-data", cwd=self.owner)
        else:
            sh("git", "merge", "--squash", "origin/scan-data", cwd=self.owner)
            sh("git", "commit", "-m", "squash pr", cwd=self.owner)
        sh("git", "push", "-q", "origin", "main", cwd=self.owner)

    def closed_pr(self, merged):
        """The newest pull request of the branch, finished, as the API reports it: its head is the branch tip it covered."""
        return {"state": "closed", "merged_at": "2026-10-02T00:00:00Z" if merged else None,
                "head": {"sha": self.remote("rev-parse", "scan-data")}}

    def owner_removes(self, path):
        sh("git", "pull", "-q", "origin", "main", cwd=self.owner)
        sh("git", "rm", "-q", path, cwd=self.owner)
        self.commit(self.owner, f"remove {path}")
        sh("git", "push", "-q", "origin", "main", cwd=self.owner)

    def owner_changes_main(self, path, text):
        sh("git", "pull", "-q", "origin", "main", cwd=self.owner)
        (self.owner / path).parent.mkdir(parents=True, exist_ok=True)
        (self.owner / path).write_text(text)
        self.commit(self.owner, f"change {path}")
        sh("git", "push", "-q", "origin", "main", cwd=self.owner)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# ---- a first scan opens the branch and the pull request, and starts CI on it
def test_a_first_scan_pushes_the_branch_opens_the_pull_request_and_dispatches_ci(world):
    gh = world.gh()
    assert world.run(gh, 1) == ("fresh", "published")
    assert world.branch_files() == ["data/state.json", "out/ai-news-2026-10-01/post.yaml"]
    assert gh.created == [("scan-data", "main", ps.TITLE)] and gh.dispatched == [("ci.yml", "scan-data")]
    assert world.remote("rev-parse", "scan-data~1") == world.remote("rev-parse", "main")          # one commit on the default branch


# ---- while the pull request is open, the scan continues from the branch (state is not lost)
def test_the_next_scan_starts_from_the_waiting_state_and_adds_to_it(world):
    gh = world.gh()
    world.run(gh, 1)
    first_tip = world.remote("rev-parse", "scan-data")
    mode = ps.prepare(gh, world.work)
    assert mode == "continue" and json.loads((world.work / "data/state.json").read_text()) == {"day": 1}      # yesterday's state is the baseline
    world.day(2)
    assert ps.publish(gh, world.work, today="2026-10-02") == "published"
    assert world.remote("rev-parse", "scan-data") != first_tip
    assert world.branch_files() == ["data/state.json", "out/ai-news-2026-10-01/post.yaml", "out/ai-news-2026-10-02/post.yaml"]
    assert len(gh.created) == 1 and len(gh.dispatched) == 2                                       # one pull request, CI on each new head


def test_a_rebuild_with_nothing_new_pushes_nothing_and_dispatches_nothing(world):
    gh = world.gh()
    world.run(gh, 1)
    tip = world.remote("rev-parse", "scan-data")
    assert world.run(gh, 0) == ("continue", "published")
    assert world.remote("rev-parse", "scan-data") == tip and len(gh.created) == 1 and len(gh.dispatched) == 1


# ---- once the pull request is merged or closed, the branch starts over
@pytest.mark.parametrize("how", ["merge", "squash"])
def test_after_a_merge_nothing_is_delivered_until_there_is_something_new(world, how):
    gh = world.gh()
    world.run(gh, 1)
    tip = world.remote("rev-parse", "scan-data")
    world.owner_merges(how)
    gh.latest = world.closed_pr(merged=True)
    assert world.run(gh, 0) == ("fresh", "nothing")
    assert world.remote("rev-parse", "scan-data") == tip and len(gh.created) == 1               # no empty pull request, no push
    world.day(3)
    assert ps.publish(gh, world.work, today="2026-10-03") == "published"
    assert world.branch_files() == ["data/state.json", "out/ai-news-2026-10-03/post.yaml"]       # only the new day, not what was merged
    assert len(gh.created) == 2                                                                  # the old pull request is finished: a new one


def test_a_closed_pull_request_drops_what_the_branch_held(world):
    gh = world.gh()
    world.run(gh, 1)
    gh.latest = world.closed_pr(merged=False)
    assert ps.prepare(gh, world.work) == "fresh"
    assert not (world.work / "out/ai-news-2026-10-01").exists()


def test_a_branch_whose_pull_request_was_never_opened_is_continued_and_gets_one(world):
    gh = world.gh(refuse_create=True)
    with pytest.raises(ps.PublishError, match="Allow GitHub Actions to create and approve pull requests"):
        world.run(gh, 1)
    assert world.branch_files()                                                                  # the data is safe on the branch
    gh = world.gh()
    assert world.run(gh, 0) == ("continue", "published")
    assert gh.created and gh.dispatched == [("ci.yml", "scan-data")]                             # opened now, and CI started on it though the head did not move


# ---- the default branch moves: the branch follows it without carrying workflow changes
def test_the_branch_is_rebuilt_on_the_new_default_branch_and_only_touches_data_and_out(world):
    gh = world.gh()
    world.run(gh, 1)
    world.owner_changes_main(".github/workflows/ci.yml", "v2\n")
    world.owner_changes_main("radar.py", "v2\n")
    world.run(gh, 2)
    assert world.remote("rev-parse", "scan-data~1") == world.remote("rev-parse", "main")         # on top of the new default branch
    assert world.branch_files() == ["data/state.json", "out/ai-news-2026-10-01/post.yaml", "out/ai-news-2026-10-02/post.yaml"]      # no workflow file in the diff


# ---- nothing to deliver
def test_a_scan_that_finds_nothing_and_a_branch_that_does_not_exist_does_nothing(world):
    gh = world.gh()
    assert world.run(gh, 0) == ("fresh", "nothing")
    assert gh.created == [] and gh.dispatched == []
    assert "scan-data" not in world.remote("branch")


# ---- failures are loud and never clobber
def test_a_refused_dispatch_is_reported_after_the_pull_request_is_open(world):
    gh = world.gh(refuse_dispatch=True)
    with pytest.raises(ps.PublishError, match="could not be dispatched"):
        world.run(gh, 1)
    assert gh.created and world.branch_files()


def test_a_branch_that_moved_after_prepare_is_not_overwritten(world):
    gh = world.gh()
    world.run(gh, 1)
    ps.prepare(gh, world.work)
    world.day(2)
    sh("git", "fetch", "-q", "origin", "scan-data", cwd=world.owner)
    sh("git", "checkout", "-q", "-B", "scan-data", "origin/scan-data", cwd=world.owner)
    (world.owner / "data/note.txt").write_text("someone else\n")
    world.commit(world.owner, "someone else")
    sh("git", "push", "-q", "origin", "scan-data", cwd=world.owner)
    moved = world.remote("rev-parse", "scan-data")
    with pytest.raises(ps.PublishError, match="push"):
        ps.publish(gh, world.work, today="2026-10-02")
    assert world.remote("rev-parse", "scan-data") == moved                                       # the lease held


def test_an_unreachable_origin_is_an_error_not_a_fresh_start(world, tmp_path):
    sh("git", "remote", "set-url", "origin", str(tmp_path / "missing.git"), cwd=world.work)
    with pytest.raises(ps.PublishError):
        ps.prepare(world.gh(), world.work)


def test_a_failed_branch_listing_is_an_error_not_a_missing_branch(world, monkeypatch):
    real = ps.git

    def flaky(*args, cwd, check=True):
        if args[0] == "ls-remote":
            return subprocess.CompletedProcess(args, 128, "", "fatal: unable to access origin")
        return real(*args, cwd=cwd, check=check)
    monkeypatch.setattr(ps, "git", flaky)
    with pytest.raises(ps.PublishError, match="cannot list"):
        ps.prepare(world.gh(), world.work)                                                     # treating it as "no branch" would start over and lose the waiting scan


def test_a_git_diff_that_fails_is_an_error_not_an_answer(world):
    with pytest.raises(ps.PublishError, match="git diff"):
        ps.differs("no-such-ref", "HEAD", cwd=world.work)
    assert ps.differs("HEAD", "HEAD", cwd=world.work) is False


def test_a_deleted_remote_branch_leaves_no_stale_expectation_on_a_reused_checkout(world):
    gh = world.gh()
    world.run(gh, 1)                                                                             # the runner's clone now remembers origin/scan-data
    sh("git", "push", "-q", "origin", "--delete", "scan-data", cwd=world.owner)                  # the owner merged and deleted the head branch
    gh = world.gh(latest={"state": "closed", "merged_at": "2026-10-02T00:00:00Z"})
    assert world.run(gh, 2) == ("fresh", "published")                                            # a self-hosted runner keeps its workspace between runs
    assert world.branch_files() == ["data/state.json", "out/ai-news-2026-10-02/post.yaml"]


# ---- a refused dispatch is retried, even when nothing else changed
def test_a_refused_dispatch_is_retried_by_the_next_run_even_with_an_identical_scan(world):
    gh = world.gh(refuse_dispatch=True)
    with pytest.raises(ps.PublishError, match="could not be dispatched"):
        world.run(gh, 1)
    gh.refuse_dispatch = False
    tip = world.remote("rev-parse", "scan-data")
    assert world.run(gh, 0) == ("continue", "published")                                         # same tree, same default branch, pull request already open
    assert gh.dispatched == [("ci.yml", "scan-data")] and world.remote("rev-parse", "scan-data") == tip      # the check gets its run, the branch is not rewritten
    assert world.run(gh, 0) == ("continue", "published") and len(gh.dispatched) == 1             # and once it has run, nothing is dispatched again


def test_a_failed_check_lookup_dispatches_to_be_safe(world):
    gh = world.gh()
    world.run(gh, 1)
    gh.refuse_lookup = True
    world.run(gh, 0)
    assert len(gh.dispatched) == 2


# ---- carrying a waiting scan brings the branch's own changes, nothing more
def test_a_file_removed_from_main_while_the_pull_request_is_open_is_not_brought_back(world):
    world.owner_changes_main("out/old/x.txt", "old issue\n")
    gh = world.gh()
    world.run(gh, 1)
    world.owner_removes("out/old/x.txt")
    world.run(gh, 2)
    assert "out/old/x.txt" not in world.remote("ls-tree", "-r", "--name-only", "scan-data").split()
    assert world.branch_files() == ["data/state.json", "out/ai-news-2026-10-01/post.yaml", "out/ai-news-2026-10-02/post.yaml"]


def test_a_file_added_to_main_while_the_pull_request_is_open_is_kept_but_is_not_part_of_the_pull_request(world):
    gh = world.gh()
    world.run(gh, 1)
    world.owner_changes_main("data/note.txt", "the owner's note\n")
    world.run(gh, 2)
    assert "data/note.txt" in world.remote("ls-tree", "-r", "--name-only", "scan-data").split()
    assert "data/note.txt" not in world.branch_files()


def test_a_waiting_scan_that_conflicts_with_main_stops_the_run_and_leaves_a_clean_tree(world):
    gh = world.gh()
    world.run(gh, 1)
    world.owner_changes_main("data/state.json", '{"day": 99}\n')                                  # the owner wrote the state file on main too
    with pytest.raises(ps.PublishError, match="conflicts with main"):
        ps.prepare(gh, world.work)
    assert sh("git", "status", "--porcelain", cwd=world.work) == "" and sh("git", "rev-parse", "HEAD", cwd=world.work) == world.remote("rev-parse", "main")


# ---- a scan pushed after the previous pull request was finished is still waiting
def test_a_scan_pushed_after_a_merged_pull_request_survives_a_failed_pull_request_creation(world):
    gh = world.gh()
    world.run(gh, 1)
    world.owner_merges("merge")
    finished = world.closed_pr(merged=True)                                                      # pull request 1 covered the tip it was merged at
    gh = world.gh(latest=finished, refuse_create=True)
    with pytest.raises(ps.PublishError, match="could not be opened"):
        world.run(gh, 2)                                                                         # day 2 is pushed, its pull request is not opened
    assert "out/ai-news-2026-10-02/post.yaml" in world.remote("ls-tree", "-r", "--name-only", "scan-data").split()
    gh = world.gh(latest=finished)                                                               # the API still answers with the old, finished pull request
    assert world.run(gh, 0) == ("continue", "published")
    assert json.loads((world.work / "data/state.json").read_text()) == {"day": 2}                # day 2's state was not thrown away
    assert world.branch_files() == ["data/state.json", "out/ai-news-2026-10-02/post.yaml"]
    assert len(gh.created) == 1                                                                  # and it gets its pull request now


def test_a_finished_pull_request_without_a_head_commit_never_discards_a_waiting_scan(world):
    gh = world.gh()
    world.run(gh, 1)
    gh.latest = {"state": "closed", "merged_at": None}                                           # nothing says which tip it covered: keep the data
    assert ps.prepare(gh, world.work) == "continue"


# ---- the branch follows main even when main's tree does not change
def test_an_empty_commit_on_main_still_moves_the_branch_onto_it(world):
    gh = world.gh()
    world.run(gh, 1)
    sh("git", "pull", "-q", "origin", "main", cwd=world.owner)
    sh("git", "commit", "-q", "--allow-empty", "-m", "empty", cwd=world.owner)
    sh("git", "push", "-q", "origin", "main", cwd=world.owner)
    world.run(gh, 0)
    assert world.remote("rev-parse", "scan-data~1") == world.remote("rev-parse", "main")         # "require up to date" would otherwise refuse the pull request


# ---- the branch name is matched exactly
def test_a_branch_that_merely_ends_in_the_same_name_is_not_the_scan_branch(world):
    sh("git", "push", "-q", "origin", "HEAD:refs/heads/foo/scan-data", cwd=world.owner)
    assert world.run(world.gh(), 1) == ("fresh", "published")


# ---- a repository without data/ or out/ is not an error
def test_a_directory_the_repository_does_not_have_is_skipped(tmp_path):
    (tmp_path / "w").mkdir()
    bare = World(tmp_path / "w", files=("data/.gitkeep", "radar.py"))
    gh = bare.gh()
    ps.prepare(gh, bare.work)
    (bare.work / "data/state.json").write_text("{}")
    assert ps.publish(gh, bare.work, today="2026-10-01") == "published"


# ---- the REST client
class Reply:
    def __init__(self, body=b""):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def client(*replies):
    seen = []

    def opener(request, timeout):
        seen.append(request)
        reply = replies[len(seen) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply
    return ps.GitHub("vohoailinh90/AI-news-radar", "tok", opener), seen


def test_the_client_asks_for_the_newest_pull_request_of_the_branch():
    gh, seen = client(Reply(json.dumps([{"state": "open"}]).encode()), Reply(b"[]"))
    assert gh.latest_pull_request("scan-data") == {"state": "open"}
    assert gh.latest_pull_request("scan-data") is None
    url = seen[0].full_url
    assert url.startswith("https://api.github.com/repos/vohoailinh90/AI-news-radar/pulls?")
    assert "head=vohoailinh90%3Ascan-data" in url and "state=all" in url and "direction=desc" in url and "per_page=1" in url
    assert seen[0].get_header("Authorization") == "Bearer tok" and seen[0].get_method() == "GET"


def test_the_client_asks_whether_a_commit_has_a_run_of_the_check():
    gh, seen = client(Reply(b'{"total_count": 1}'), Reply(b'{"total_count": 0}'), Reply(b"null"))
    assert gh.has_check_run("abc123", "tests") is True
    assert gh.has_check_run("abc123", "tests") is False
    assert gh.has_check_run("abc123", "tests") is False
    assert "/commits/abc123/check-runs?" in seen[0].full_url and "check_name=tests" in seen[0].full_url and seen[0].get_method() == "GET"


def test_the_client_opens_a_pull_request_and_dispatches_a_workflow():
    gh, seen = client(Reply(b'{"number": 7}'), Reply(b""))
    assert gh.create_pull_request("scan-data", "main", "T", "B") == {"number": 7}
    gh.dispatch("ci.yml", "scan-data")
    assert seen[0].get_method() == "POST" and seen[0].full_url.endswith("/repos/vohoailinh90/AI-news-radar/pulls")
    assert json.loads(seen[0].data) == {"head": "scan-data", "base": "main", "title": "T", "body": "B"}
    assert seen[1].full_url.endswith("/actions/workflows/ci.yml/dispatches") and json.loads(seen[1].data) == {"ref": "scan-data"}


def test_the_client_turns_http_and_network_errors_into_github_errors():
    refusal = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b'{"message": "not permitted"}'))
    gh, _ = client(refusal, urllib.error.URLError("no route"), urllib.error.HTTPError("u", 502, "Bad", {}, io.BytesIO(b"<html>")))
    with pytest.raises(ps.GitHubError) as first:
        gh.create_pull_request("a", "b", "t", "b")
    assert first.value.status == 403 and "not permitted" in str(first.value)
    with pytest.raises(ps.GitHubError) as second:
        gh.dispatch("ci.yml", "a")
    assert second.value.status == 0 and "no route" in str(second.value)
    with pytest.raises(ps.GitHubError) as third:
        gh.latest_pull_request("a")
    assert third.value.status == 502


def test_main_runs_the_step_it_is_given_on_the_branch_it_is_given(world, monkeypatch, capsys):
    gh = world.gh()
    monkeypatch.setattr(ps, "GitHub", lambda repo, token: gh)
    env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t"}
    assert ps.main(["prepare", "--branch", "other-branch"], env=env, cwd=world.work) == 0
    assert "other-branch: fresh" in capsys.readouterr().out and sh("git", "branch", "--show-current", cwd=world.work) == "other-branch"
    world.day(1)
    assert ps.main(["publish", "--branch", "other-branch", "--ci-workflow", "other.yml"], env=env, cwd=world.work) == 0
    assert gh.created[0][0] == "other-branch" and gh.dispatched == [("other.yml", "other-branch")]
    gh.reported.clear()                                                                          # the lookup is by the name given
    seen = []
    gh.has_check_run = lambda sha, name: seen.append(name) or True
    assert ps.main(["publish", "--branch", "other-branch", "--check", "build"], env=env, cwd=world.work) == 0 and seen == ["build"]
    gh.refuse_dispatch = True
    world.day(2)
    assert ps.main(["publish", "--branch", "other-branch"], env=env, cwd=world.work) == 1
    assert "::error::" in capsys.readouterr().out


def test_main_refuses_to_run_without_the_workflow_environment(capsys):
    assert ps.main(["publish"], env={}) == 1
    assert "::error::" in capsys.readouterr().out


# ---- the workflow itself
def workflow(name):
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def test_the_scan_workflow_prepares_scans_then_publishes_and_never_pushes_by_itself():
    wf = workflow("scan.yml")
    job = wf["jobs"]["scan"]
    runs = [step.get("run", "") for step in job["steps"]]
    prepare = next(i for i, r in enumerate(runs) if "publish_scan.py prepare" in r)
    scan = next(i for i, r in enumerate(runs) if "radar.py run" in r)
    publish = next(i for i, r in enumerate(runs) if "publish_scan.py publish" in r)
    assert prepare < scan < publish
    assert not any("git push" in r for r in runs)
    assert {"contents": "write", "pull-requests": "write", "actions": "write", "checks": "read"}.items() <= wf["permissions"].items()
    checkout = next(step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["fetch-depth"] == 0
    env = {**wf.get("env", {}), **job.get("env", {})}
    assert "github.token" in env["GITHUB_TOKEN"]
    assert job["runs-on"] == "${{ vars.CI_RUNNER || 'ubuntu-latest' }}"


def test_ci_can_be_dispatched_on_the_scan_branch():
    wf = workflow("ci.yml")
    triggers = wf.get("on", wf.get(True))
    assert "workflow_dispatch" in triggers and "pull_request" in triggers
    assert any(step.get("run") == "python -m pytest -q" for step in wf["jobs"]["tests"]["steps"])        # the job a branch rule requires is still called `tests`


# ---- the scan step's exit codes: only a real failure may stop the run before the scan is saved
def run_scan_step(tmp_path, code):
    """Execute the workflow's own "Scan and export" script the way Actions does (bash -eo pipefail), with `python` stubbed to exit with `code`."""
    step = next(step for step in workflow("scan.yml")["jobs"]["scan"]["steps"] if step.get("name") == "Scan and export")
    shim = tmp_path / "bin"
    shim.mkdir(exist_ok=True)
    (shim / "python").write_text(f"#!/bin/sh\nexit {code}\n")
    (shim / "python").chmod(0o755)
    done = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]], capture_output=True, text=True,
                          env={"PATH": f"{shim}:/usr/bin:/bin"}, cwd=tmp_path)
    return done.returncode, done.stdout


@pytest.mark.parametrize("code, expected", [(0, 0), (3, 0), (4, 0), (2, 2), (5, 5), (1, 1)])
def test_the_scan_step_fails_only_when_the_scan_itself_could_not_be_trusted(tmp_path, code, expected):
    assert run_scan_step(tmp_path, code)[0] == expected
    # 3: nothing worth posting. 4: today's issue already exists (a second run the same local day): the scan was saved and its
    # events wait in data/last-scan.json for the next issue, so the run must go on to publish them instead of discarding them.
    # 2 (every source failed) and 5 (a post with link-looking text) stay red: the owner has to see those.


def test_a_refused_export_says_so_in_the_log(tmp_path):
    assert "stay pending" in run_scan_step(tmp_path, 4)[1]


def test_a_refused_export_leaves_the_scan_saved_and_its_events_wait_for_the_next_issue(tmp_path):
    """The premise of the exit-4 handling above: `run` is scan then export, so when export is refused the scan is already on disk."""
    root = make_root(tmp_path)
    promo = Event(kind="free-promo", vendor="openai", topic="free", title="Codex is free for one week", url="https://e.org", source="aider-polyglot",
                  source_name="A", trust="primary", when=NOW, item_id="promo", score=12)
    cli.save_scan(root, [promo], [], NOW)
    (root / "out" / "ai-news-2026-10-14" / "tools" / "ai-news-2026-10-14").mkdir(parents=True)          # today's issue (22:00 UTC is the 14th in Vietnam) exists
    fetch = fetcher_for({"aider-polyglot": "aider_polyglot_leaderboard.yml"})
    cli.scan(root, fetcher=fetch, now=NOW)
    assert cli.cmd_export(argparse.Namespace(date=None, studio=None, force=False), root) == 4              # refused: nothing is exported...
    assert (root / "data" / "state.json").exists() and "promo" in [e.item_id for e in cli.load_pending(root)]      # ...and the scan and its event are saved
    tomorrow = NOW + timedelta(days=1)
    cli.scan(root, fetcher=fetch, now=tomorrow)
    assert cli.cmd_export(argparse.Namespace(date=None, studio=None, force=False), root) == 0
    assert "Codex is free for one week" in (root / "out" / "ai-news-2026-10-15" / "REPORT.md").read_text(encoding="utf-8")     # the next issue carries it
