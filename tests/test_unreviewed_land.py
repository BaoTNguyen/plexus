"""A review that never ran must not count as approval.

Drives plexus.run._walk directly, with heart's episode dispatch stubbed out:
what's under test is the land decision at the review gate, not heart itself.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("EVENT_JOURNAL_DIR", tempfile.mkdtemp(prefix="plexus-journal-"))

here = Path(__file__).resolve()
for p in (here.parents[1] / "src", here.parents[2] / "heart" / "src"):
    sys.path.insert(0, str(p))

from plexus import ledger  # noqa: E402
from plexus.spec import GoalSpec  # noqa: E402
from plexus import run as run_mod  # noqa: E402


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                    capture_output=True, text=True)


def _make_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "a")
    (repo / "README.md").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _add_diff(repo, name="foo.txt", content="hello\n") -> str:
    """A real, applicable patch adding one file, without touching the tree."""
    (repo / name).write_text(content)
    _git(repo, "add", name)
    diff = subprocess.run(["git", "-C", str(repo), "diff", "--cached"],
                          capture_output=True, text=True, check=True).stdout
    _git(repo, "reset", "--")
    (repo / name).unlink()
    return diff


def _write_plan(root: Path, feat: dict) -> None:
    plans = root / ".plexus"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / "plan.jsonl").write_text(json.dumps(feat) + "\n")


def _spec(**over) -> GoalSpec:
    base = dict(
        goal_id="g1", text="t", context="", suite="true",
        attempts_per_feature=3, episodes_per_goal=25,
        agent="claude", agent_cmd=None, timeout=60, spec_hash="deadbeef",
        pipeline=True, network="api", review_hold=(), pr_base="",
    )
    base.update(over)
    return GoalSpec(**base)


def _episode(root, runs_dir, ep_id, diff, review_verdict, roles=()):
    (root / runs_dir / ep_id).mkdir(parents=True, exist_ok=True)
    (root / runs_dir / ep_id / "diff.patch").write_text(diff)
    return {
        "episode_id": ep_id, "outcome": "pass",
        "review_verdict": review_verdict, "roles": list(roles),
    }


@pytest.fixture
def repo(tmp_path):
    return _make_repo(tmp_path)


@pytest.fixture(autouse=True)
def stub_heart(monkeypatch):
    """Heart's episode dispatch isn't under test; only the land gate is."""
    monkeypatch.setattr(run_mod, "_episode_cost", lambda cands: {})
    monkeypatch.setattr(run_mod, "best_episode", lambda cands: cands[0])
    monkeypatch.setattr(run_mod.scope, "observe", lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "_run_acceptance", lambda *a, **k: (True, "", ""))
    monkeypatch.setattr(run_mod._tasks, "update", lambda *a, **k: None)


def _run_feature(monkeypatch, repo, ep, feat=None):
    feat = feat or {"id": "f1", "title": "t", "spec": "do a thing",
                    "acceptance": "true", "touches": None}
    _write_plan(repo, feat)
    ledger.record("plan.approved", goal_id="g1", root=repo, plan_id="")
    monkeypatch.setattr(run_mod, "_build", lambda *a, **k: [ep])
    spec = _spec()
    code = run_mod._walk(spec, repo, "runs", 1, "")
    return code, ledger.read(repo)


def test_review_never_started_holds(monkeypatch, repo):
    ep = _episode(repo, "runs", "ep-1", _add_diff(repo), review_verdict=None, roles=[])
    code, recs = _run_feature(monkeypatch, repo, ep)
    assert code == 1
    esc = [r for r in recs if r["kind"] == "escalation.raised"]
    assert len(esc) == 1
    assert esc[0]["reason_class"] == "held_for_review"
    assert "did not run" in esc[0]["reason"]
    assert not any(r["kind"] == "feature.landed" for r in recs)


def test_review_timed_out_holds(monkeypatch, repo):
    roles = [{"role": "review.1", "agent": "claude", "exit_code": 0, "timed_out": True}]
    ep = _episode(repo, "runs", "ep-2", _add_diff(repo), review_verdict=None, roles=roles)
    code, recs = _run_feature(monkeypatch, repo, ep)
    assert code == 1
    esc = [r for r in recs if r["kind"] == "escalation.raised"]
    assert len(esc) == 1
    assert esc[0]["reason_class"] == "held_for_review"
    assert "timed out" in esc[0]["reason"]
    assert not any(r["kind"] == "feature.landed" for r in recs)


def test_review_approved_still_lands(monkeypatch, repo):
    roles = [{"role": "review.1", "agent": "claude", "exit_code": 0, "timed_out": False}]
    ep = _episode(repo, "runs", "ep-3", _add_diff(repo), review_verdict="approve", roles=roles)
    code, recs = _run_feature(monkeypatch, repo, ep)
    assert code == 0
    assert any(r["kind"] == "feature.landed" for r in recs)
    assert not any(r["kind"] == "escalation.raised" for r in recs)


def test_review_rejected_still_fails(monkeypatch, repo):
    roles = [{"role": "review.1", "agent": "claude", "exit_code": 0, "timed_out": False}]
    ep = _episode(repo, "runs", "ep-4", _add_diff(repo), review_verdict="reject", roles=roles)
    code, recs = _run_feature(monkeypatch, repo, ep)
    assert not any(r["kind"] == "feature.landed" for r in recs)
    failed = [r for r in recs if r["kind"] == "feature.failed"]
    assert failed and failed[-1]["failure_class"] == "review_rejected"


def test_solo_mode_unreviewed_still_lands(monkeypatch, repo):
    """Unchanged behavior: no review role means no reviewer was ever promised."""
    ep = _episode(repo, "runs", "ep-5", _add_diff(repo), review_verdict=None, roles=[])
    feat = {"id": "f1", "title": "t", "spec": "do a thing",
            "acceptance": "true", "touches": None}
    _write_plan(repo, feat)
    ledger.record("plan.approved", goal_id="g1", root=repo, plan_id="")
    monkeypatch.setattr(run_mod, "_build", lambda *a, **k: [ep])
    spec = _spec(pipeline=False)
    code = run_mod._walk(spec, repo, "runs", 1, "")
    recs = ledger.read(repo)
    assert code == 0
    assert any(r["kind"] == "feature.landed" for r in recs)
    assert not any(r["kind"] == "escalation.raised" for r in recs)
