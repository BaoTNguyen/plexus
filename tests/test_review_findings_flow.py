"""Review findings end to end: landing with concerns, a rejection's findings
carried into the next attempt, and a resolved hold landing what was signed off.

No real network/db/git against the checkout: each test builds its own scratch
git repo under tmp_path and chdirs there first. `heart`'s sandbox dispatch
(`_build_sandbox_retrying`) and plexus's own acceptance runner
(`_run_acceptance`) are stubbed so the only thing exercised is `plexus.run`'s
own ledger/retry_context bookkeeping -- everything downstream of "an episode
came back" is real: git apply, git commit, `plexus.diagnose.why`, and a second
real `run.run()` call reading the ledger the first one wrote.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from plexus import diagnose, ledger, run
from plexus import vascular_state
from plexus.spec import GoalSpec


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@example.com"],
                   check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    (path / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "base"], check=True)


def _diff_adding_file(repo: Path, rel: str, content: str) -> str:
    """A real `git diff --cached` patch that adds `rel`, leaving `repo` back at
    its prior HEAD so every attempt in a test can reuse the same patch."""
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", rel], check=True, capture_output=True)
    diff = subprocess.run(["git", "-C", str(repo), "diff", "--cached"],
                          check=True, capture_output=True, text=True).stdout
    # `reset --hard` alone unstages and removes `rel` (new in the index, absent
    # from HEAD) -- no `clean -fd`, which would also sweep plexus's own
    # untracked `.vascular/plexus/` state out of the working tree.
    subprocess.run(["git", "-C", str(repo), "reset", "--hard", "HEAD"],
                   check=True, capture_output=True)
    return diff


def _write_plan(root: Path, feat: dict, plan_id: str = "plan-1") -> None:
    p = vascular_state.plexus_dir(root) / "plan.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"plan_id": plan_id, "task_id": "", **feat}) + "\n")


def _approve_plan(spec: GoalSpec, root: Path, plan_id: str = "plan-1") -> None:
    ledger.record("plan.approved", goal_id=spec.goal_id, root=root, plan_id=plan_id,
                  task="", approver="test", waived=[])


def _feature(fid: str, touches: str) -> dict:
    return {
        "id": fid, "title": fid, "spec": f"build {fid}", "acceptance": "true",
        "touches": [touches], "contract": [], "priority": 0, "depends_on": [],
        "needs_upstream": [], "skills": [], "manual_checks": [],
        "difficulty": "easy", "effort": "low",
    }


def _spec(goal_id: str, **overrides) -> GoalSpec:
    fields = dict(
        goal_id=goal_id, text="t", context="", suite="false",
        attempts_per_feature=2, episodes_per_goal=25, agent="claude",
        agent_cmd=None, timeout=30, spec_hash="hash", pipeline=False,
        orchestrate=False, network="api", review_hold=(), pr_base="",
    )
    fields.update(overrides)
    return GoalSpec(**fields)


def _fake_build_sequence(cands: list[tuple]):
    """Each tuple is (episode_id, outcome, review_verdict, review_findings,
    diff_text); consumed one per `_build_sandbox_retrying` call, in order."""
    it = iter(cands)

    def _build(spec_, task, candidates, roles, runs_dir, goal_id, feature_id,
               attempt, root):
        ep_id, outcome, review, findings, diff_text = next(it)
        d = runs_dir / ep_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "diff.patch").write_text(diff_text)
        ep = {"episode_id": ep_id, "outcome": outcome}
        if review is not None:
            ep["review_verdict"] = review
        if findings is not None:
            ep["review_findings"] = findings
        return [ep]

    return _build


@pytest.fixture(autouse=True)
def _scratch_journal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EVENT_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setattr(run, "_run_acceptance", lambda *a, **k: (True, "", ""))


def test_landed_approve_with_findings_writes_review_concern_and_why_lists_it(
        tmp_path, monkeypatch):
    repo = tmp_path / "goal"
    _init_repo(repo)
    feat = _feature("f1", "feature.txt")
    _write_plan(repo, feat)
    spec = _spec("g1")
    _approve_plan(spec, repo)

    diff = _diff_adding_file(repo, "feature.txt", "hi\n")
    findings = [
        {"severity": "concern", "file": "src/a.py", "line": 12, "claim": "needs a test"},
        {"severity": "blocker", "file": "src/b.py", "line": 34, "claim": "unsafe default"},
    ]
    monkeypatch.setattr(run, "_build_sandbox_retrying",
                        _fake_build_sequence([("ep-1", "pass", "approve", findings, diff)]))

    # spec.suite is "false": after landing, the goal-level scope gate fails and
    # run() escalates (1) rather than finishing the goal (0) -- irrelevant to
    # what this test checks, and it sidesteps run()'s task-tracking tail (which
    # assumes a `plexus.tasks` task governs the run; ours doesn't name one).
    assert run.run(spec, root=repo) == 1

    recs = ledger.read(repo)
    concerns = [r for r in recs if r["kind"] == "review.concern"]
    seen = {(c["feature_id"], c["severity"], c["file"], c["line"], c["claim"])
            for c in concerns}
    assert seen == {
        ("f1", "concern", "src/a.py", 12, "needs a test"),
        ("f1", "blocker", "src/b.py", 34, "unsafe default"),
    }

    out = "\n".join(diagnose.why(str(repo), "f1"))
    assert "review concern [concern] src/a.py:12: needs a test" in out
    assert "review concern [blocker] src/b.py:34: unsafe default" in out


def test_review_rejected_carries_findings_into_next_attempts_retry_context(
        tmp_path, monkeypatch):
    repo = tmp_path / "goal"
    _init_repo(repo)
    feat = _feature("f2", "feature.txt")
    _write_plan(repo, feat)
    spec = _spec("g2", attempts_per_feature=2)
    _approve_plan(spec, repo)

    diff = _diff_adding_file(repo, "feature.txt", "hi\n")
    rejecting = [{"severity": "blocker", "file": "src/x.py", "line": 5,
                 "claim": "breaks invariant"}]
    monkeypatch.setattr(run, "_build_sandbox_retrying", _fake_build_sequence([
        ("ep-r1", "pass", "reject", rejecting, diff),
        ("ep-r2", "pass", "approve", [], diff),
    ]))

    assert run.run(spec, root=repo) == 1  # lands on attempt 2, then the (failing) suite escalates

    started = [r for r in ledger.read(repo)
              if r["kind"] == "feature.started" and r["feature_id"] == "f2"]
    assert len(started) == 2
    retry_context = started[1]["retry_context"]
    assert retry_context["review_findings"] == rejecting


def _hold_then_resolve(tmp_path, monkeypatch, fid="f3"):
    repo = tmp_path / "goal"
    _init_repo(repo)
    _write_plan(repo, _feature(fid, "feature.txt"))
    # the feature's risk class (review.classify == "leaf") is held for sign-off
    spec = _spec("g3", review_hold=("leaf",))
    _approve_plan(spec, repo)
    diff = _diff_adding_file(repo, "feature.txt", "signed off\n")
    monkeypatch.setattr(run, "_build_sandbox_retrying",
                        _fake_build_sequence([("ep-h1", "pass", None, None, diff)]))
    assert run.run(spec, root=repo) == 1
    raised = [r for r in ledger.read(repo) if r["kind"] == "escalation.raised"]
    assert [r["reason_class"] for r in raised] == ["held_for_review"]
    ledger.record("escalation.resolved", goal_id=spec.goal_id, feature_id=fid,
                  root=repo, resolution="sign-off")

    def _no_build(*a, **k):
        raise AssertionError("a resolved hold must land the held diff, not rebuild")
    monkeypatch.setattr(run, "_build_sandbox_retrying", _no_build)
    return repo, spec


def test_resolved_hold_lands_the_signed_off_diff_without_rebuilding(
        tmp_path, monkeypatch):
    repo, spec = _hold_then_resolve(tmp_path, monkeypatch)
    assert run.run(spec, root=repo) == 1  # lands, then the (failing) suite escalates

    recs = ledger.read(repo)
    landed = next(r for r in recs if r["kind"] == "feature.landed")
    assert landed["from_held"] == "ep-h1" and landed["episode_id"] == "ep-h1"
    assert (repo / "feature.txt").read_text() == "signed off\n"
    assert sum(r["kind"] == "feature.started" for r in recs) == 1


def test_resolved_hold_whose_diff_no_longer_passes_is_held_again(
        tmp_path, monkeypatch):
    repo, spec = _hold_then_resolve(tmp_path, monkeypatch)
    monkeypatch.setattr(run, "_run_acceptance",
                        lambda *a, **k: (False, "patch does not apply", "acceptance"))
    assert run.run(spec, root=repo) == 1

    recs = ledger.read(repo)
    assert not any(r["kind"] == "feature.landed" for r in recs)
    assert recs[-1]["kind"] == "escalation.raised"
    assert recs[-1]["reason_class"] == "held_stale"
    assert "patch does not apply" in recs[-1]["reason"]
    assert not (repo / "feature.txt").exists()

    # resolving the stale hold rebuilds -- and the rebuild is held, not landed
    ledger.record("escalation.resolved", goal_id=spec.goal_id, feature_id="f3",
                  root=repo, resolution="rebuild")
    monkeypatch.setattr(run, "_run_acceptance", lambda *a, **k: (True, "", ""))
    diff = _diff_adding_file(repo, "feature.txt", "rebuilt\n")
    monkeypatch.setattr(run, "_build_sandbox_retrying",
                        _fake_build_sequence([("ep-h2", "pass", None, None, diff)]))
    assert run.run(spec, root=repo) == 1
    last = ledger.read(repo)[-1]
    assert last["reason_class"] == "held_for_review" and last["episode_ids"] == ["ep-h2"]
    assert not (repo / "feature.txt").exists()
