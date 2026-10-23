"""Every escalation that ends an attempt carries that attempt's cost, and a
held attempt is counted once: on the hold, never again on its from_held land.

Drives plexus.run._walk with heart's build stubbed, like test_unreviewed_land.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

here = Path(__file__).resolve()
for p in (here.parents[1] / "src", here.parents[2] / "heart" / "src"):
    sys.path.insert(0, str(p))

from plexus import ledger  # noqa: E402
from plexus import vascular_state  # noqa: E402
from plexus.spec import GoalSpec  # noqa: E402
from plexus import run as run_mod  # noqa: E402

USAGE = {"cost_usd": 0.25, "tokens_in": 10, "tokens_out": 5}
FEAT = {"id": "f1", "title": "t", "spec": "do a thing", "acceptance": "true",
        "touches": None}


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "a")
    (repo / "README.md").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    monkeypatch.setattr(run_mod, "best_episode", lambda cands: cands[0])
    monkeypatch.setattr(run_mod.scope, "observe", lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "_run_acceptance", lambda *a, **k: (True, "", ""))
    monkeypatch.setattr(run_mod._tasks, "update", lambda *a, **k: None)
    return repo


def _diff(repo) -> str:
    (repo / "foo.txt").write_text("hello\n")
    _git(repo, "add", "foo.txt")
    diff = _git(repo, "diff", "--cached")
    _git(repo, "reset", "--")
    (repo / "foo.txt").unlink()
    return diff


def _walk(monkeypatch, repo, feat, review_hold=(), **ep):
    ep_dir = repo / "runs" / "ep-1"
    ep_dir.mkdir(parents=True, exist_ok=True)
    (ep_dir / "diff.patch").write_text(_diff(repo))
    episode = {"episode_id": "ep-1", "outcome": "pass", "review_verdict": None,
               "roles": [], "usage": USAGE, **ep}
    monkeypatch.setattr(run_mod, "_build", lambda *a, **k: [episode])
    spec = GoalSpec(goal_id="g1", text="t", context="", suite="true",
                    attempts_per_feature=3, episodes_per_goal=25, agent="claude",
                    agent_cmd=None, timeout=60, spec_hash="deadbeef",
                    pipeline=False, network="api", review_hold=review_hold,
                    pr_base="")
    return run_mod._walk(spec, repo, "runs", 1, "")


def _start(repo, feat):
    plans = vascular_state.plexus_dir(repo)
    plans.mkdir(parents=True, exist_ok=True)
    (plans / "plan.jsonl").write_text(json.dumps(feat) + "\n")
    ledger.record("plan.approved", goal_id="g1", root=repo, plan_id="")


def _last_escalation(repo):
    return [r for r in ledger.read(repo) if r["kind"] == "escalation.raised"][-1]


def _priced(esc):
    assert esc["cost_usd"] == 0.25
    assert esc["tokens_in"] == 10 and esc["tokens_out"] == 5


def test_blocked_escalation_carries_cost(monkeypatch, repo):
    _start(repo, FEAT)
    assert _walk(monkeypatch, repo, FEAT, outcome="blocked",
                 blocked_reason="which db?") == 1
    esc = _last_escalation(repo)
    assert esc["reason_class"] == "blocked_on_decision"
    _priced(esc)


def test_scope_violation_carries_cost(monkeypatch, repo):
    feat = {**FEAT, "touches": ["other.txt"]}
    _start(repo, feat)
    assert _walk(monkeypatch, repo, feat) == 1
    esc = _last_escalation(repo)
    assert esc["reason_class"] == "scope_violation"
    _priced(esc)


def test_held_counts_once(monkeypatch, repo):
    _start(repo, FEAT)
    hold = (run_mod.classify(FEAT),)
    assert _walk(monkeypatch, repo, FEAT, review_hold=hold) == 1
    esc = _last_escalation(repo)
    assert esc["reason_class"] == "held_for_review"
    _priced(esc)
    assert esc["held_cost"]["cost_usd"] == 0.25

    assert ledger.resolve(repo, "g1", "f1", "ship it") is None
    assert _walk(monkeypatch, repo, FEAT, review_hold=hold) == 0
    recs = ledger.read(repo)
    landed = [r for r in recs if r["kind"] == "feature.landed"]
    assert len(landed) == 1 and landed[0]["from_held"] == "ep-1"
    assert "cost_usd" not in landed[0]
    assert sum(r.get("cost_usd") or 0 for r in recs) == 0.25


def test_seat_exhausted_carries_cost(monkeypatch, repo):
    _start(repo, FEAT)
    log = repo / "runs" / "ep-1" / "implement.log"
    log.parent.mkdir(parents=True)
    log.write_text("work\nYou've hit your weekly limit · resets 12am (UTC)\n")
    assert _walk(monkeypatch, repo, FEAT,
                 roles=[{"role": "implement", "agent": "claude"}]) == 1
    esc = _last_escalation(repo)
    assert esc["reason_class"] == "seat_exhausted"
    _priced(esc)
    recs = ledger.read(repo)
    assert not any(r["kind"] == "feature.failed" for r in recs)
    assert sum(r.get("cost_usd") or 0 for r in recs) == 0.25
