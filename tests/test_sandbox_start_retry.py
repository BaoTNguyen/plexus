"""A sandbox that fails to even start is an infra flake, not a coding
failure: it must not spend the feature's attempt budget or masquerade as a
`feature.failed`. Drives plexus.run._walk directly, with heart's episode
dispatch stubbed out — what's under test is the retry/escalation wiring
around `_build`, not heart itself.
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


def _write_plan(root: Path, feat: dict, task_id: str = "") -> None:
    plans = root / ".plexus"
    if task_id:
        plans = plans / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    name = f"{task_id}.jsonl" if task_id else "plan.jsonl"
    (plans / name).write_text(json.dumps(feat) + "\n")


def _spec(**over) -> GoalSpec:
    base = dict(
        goal_id="g1", text="t", context="", suite="true",
        attempts_per_feature=3, episodes_per_goal=25,
        agent="claude", agent_cmd=None, timeout=60, spec_hash="deadbeef",
        pipeline=False, network="api", review_hold=(), pr_base="",
    )
    base.update(over)
    return GoalSpec(**base)


def _episode(root, runs_dir, ep_id, diff, review_verdict=None, roles=()):
    (root / runs_dir / ep_id).mkdir(parents=True, exist_ok=True)
    (root / runs_dir / ep_id / "diff.patch").write_text(diff)
    return {
        "episode_id": ep_id, "outcome": "pass",
        "review_verdict": review_verdict, "roles": list(roles),
    }


_FEAT = {"id": "f1", "title": "t", "spec": "do a thing",
         "acceptance": "true", "touches": None}


@pytest.fixture
def repo(tmp_path):
    return _make_repo(tmp_path)


@pytest.fixture(autouse=True)
def stub_heart(monkeypatch):
    """Heart's episode dispatch isn't under test; only the retry/escalation
    wiring around it is."""
    monkeypatch.setattr(run_mod, "_episode_cost", lambda cands: {})
    monkeypatch.setattr(run_mod, "best_episode", lambda cands: cands[0])
    monkeypatch.setattr(run_mod.scope, "observe", lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "_run_acceptance", lambda *a, **k: (True, "", ""))
    monkeypatch.setattr(run_mod._tasks, "update", lambda *a, **k: None)


def _prime(repo):
    _write_plan(repo, _FEAT)
    ledger.record("plan.approved", goal_id="g1", root=repo, plan_id="")


def test_a_sandbox_start_failure_is_retried_without_spending_an_attempt(monkeypatch, repo):
    ep = _episode(repo, "runs", "ep-1", _add_diff(repo))
    _prime(repo)
    calls = {"n": 0}

    def fake_build(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("sandbox failed to start: boom")
        return [ep]

    monkeypatch.setattr(run_mod, "_build", fake_build)
    code = run_mod._walk(_spec(), repo, "runs", 1, "")
    recs = ledger.read(repo)

    assert code == 0
    assert calls["n"] == 2
    assert any(r["kind"] == "feature.landed" for r in recs)
    assert not any(r["kind"] == "feature.failed" for r in recs)
    sandbox_recs = [r for r in recs if r["kind"] == "sandbox.start_failed"]
    assert len(sandbox_recs) == 1
    assert sandbox_recs[0]["goal_id"] == "g1"
    assert sandbox_recs[0]["feature_id"] == "f1"
    assert sandbox_recs[0]["attempt"] == 1
    assert sandbox_recs[0]["error"].endswith("boom")
    started = [r for r in recs if r["kind"] == "feature.started"]
    assert len(started) == 1  # the retry never re-recorded a new attempt


def test_a_sandbox_that_never_starts_escalates_and_blocks(monkeypatch, repo):
    _prime(repo)
    calls = {"n": 0}

    def fake_build(*a, **k):
        calls["n"] += 1
        raise RuntimeError(f"sandbox failed to start: attempt {calls['n']}")

    monkeypatch.setattr(run_mod, "_build", fake_build)
    code = run_mod._walk(_spec(), repo, "runs", 1, "")
    recs = ledger.read(repo)

    assert code == 1
    assert calls["n"] == 3  # exactly 3 tries, no more
    esc = [r for r in recs if r["kind"] == "escalation.raised"]
    assert len(esc) == 1
    assert esc[0]["reason_class"] == "sandbox_unavailable"
    assert "attempt 3" in esc[0]["reason"]
    assert not any(r["kind"] == "feature.failed" for r in recs)
    assert len([r for r in recs if r["kind"] == "sandbox.start_failed"]) == 3

    # the attempt that never got a turn must not count against the budget
    state, next_attempt, budget_used = run_mod._feature_state(recs, "g1", "f1")
    assert state == "escalated"
    ledger.record("escalation.resolved", goal_id="g1", feature_id="f1",
                  root=repo, resolution="sandbox is back")
    recs = ledger.read(repo)
    state, next_attempt, budget_used = run_mod._feature_state(recs, "g1", "f1")
    assert state == "open"
    assert budget_used == 0


def test_run_leaves_the_task_blocked_not_running_on_sandbox_exhaustion(monkeypatch, repo):
    _write_plan(repo, _FEAT, task_id="t1")
    ledger.record("plan.approved", goal_id="g1", root=repo, plan_id="")
    monkeypatch.setattr(run_mod, "_build",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("sandbox failed to start: down")))
    updates = []
    monkeypatch.setattr(run_mod._tasks, "update",
                        lambda root, tid, **kw: updates.append(kw))
    monkeypatch.setattr(run_mod._tasks, "read", lambda root: [])

    code = run_mod.run(_spec(), root=repo, runs_dir="runs", candidates=1, task_id="t1")

    assert code == 1
    assert updates[0] == {"state": "running", "error": ""}
    assert updates[-1]["state"] == "blocked"
    assert "sandbox" in updates[-1]["error"]
    assert not any(u["state"] == "running" for u in updates[1:])


def test_a_non_sandbox_runtime_error_still_propagates(monkeypatch, repo):
    _prime(repo)

    def fake_build(*a, **k):
        raise RuntimeError("something else entirely")

    monkeypatch.setattr(run_mod, "_build", fake_build)
    with pytest.raises(RuntimeError, match="something else entirely"):
        run_mod._walk(_spec(), repo, "runs", 1, "")
    recs = ledger.read(repo)
    assert not any(r["kind"] == "sandbox.start_failed" for r in recs)
    assert not any(r["kind"] == "escalation.raised" for r in recs)


# --- seat exhaustion -------------------------------------------------------

_SEAT_MUST_MATCH = [
    "You've hit your weekly limit · resets 12am (UTC)",
    "You've hit your org's monthly spend limit · ask your admin to raise "
    "it at claude.ai/admin-settings/usage · your session limit resets "
    "8am (UTC)",
    "You have hit a usage limit, resets 3:15pm",
    "rate limit hit\ntry again at Oct 3",
    "usage limit reached, resets in 30 minutes",
]

_SEAT_MUST_NOT_MATCH = [
    "You've hit your daily quota, come back tomorrow",     # no "limit" + reset time
    "You've hit your weekly limit, nothing to see here",   # no reset time at all
    "rate limit; try again in a bit",                      # not a time expression
    "rate limit hit, retry later",                         # no time expression
]


def test_seat_must_match_examples_extract_the_reset_text():
    assert run_mod._seat_exhaustion(_SEAT_MUST_MATCH[0]) == "12am (UTC)"
    assert run_mod._seat_exhaustion(_SEAT_MUST_MATCH[1]) == "8am (UTC)"
    assert run_mod._seat_exhaustion(_SEAT_MUST_MATCH[2]) == "3:15pm"
    assert run_mod._seat_exhaustion(_SEAT_MUST_MATCH[3]) == "Oct 3"
    assert run_mod._seat_exhaustion(_SEAT_MUST_MATCH[4]) is not None  # a reset time was found
    # not-time cases from the spec must stay unmatched regardless of trigger phrase
    assert run_mod._seat_exhaustion("rate limit; try again in a bit") is None


def test_seat_must_not_match_examples_stay_unmatched():
    for text in _SEAT_MUST_NOT_MATCH:
        assert run_mod._seat_exhaustion(text) is None


def _write_role_log(root, runs_dir, ep_id, role, text):
    out = root / runs_dir / ep_id
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{role}.log").write_text(text)


def _pipeline_episode(root, runs_dir, ep_id, diff, *, implement_log=""):
    (root / runs_dir / ep_id).mkdir(parents=True, exist_ok=True)
    (root / runs_dir / ep_id / "diff.patch").write_text(diff)
    for role, agent, log in (
        ("implement", "claude", implement_log),
        ("test", "claude", ""),
        ("review", "claude", ""),
    ):
        _write_role_log(root, runs_dir, ep_id, role, log)
    return {
        "episode_id": ep_id, "outcome": "pass", "review_verdict": "approve",
        "roles": [{"role": "implement", "agent": "claude"},
                  {"role": "test", "agent": "claude"},
                  {"role": "review", "agent": "claude"}],
    }


def test_seat_exhaustion_in_a_pipeline_episode_stops_the_run(monkeypatch, repo):
    ep = _pipeline_episode(repo, "runs", "ep-1", _add_diff(repo),
                           implement_log=_SEAT_MUST_MATCH[0])
    _prime(repo)
    monkeypatch.setattr(run_mod, "_build", lambda *a, **k: [ep])

    code = run_mod._walk(_spec(pipeline=True), repo, "runs", 1, "")
    recs = ledger.read(repo)

    assert code == 1
    assert not any(r["kind"] == "feature.failed" for r in recs)
    seat_recs = [r for r in recs if r["kind"] == "seat.exhausted"]
    assert len(seat_recs) == 1
    assert seat_recs[0]["goal_id"] == "g1"
    assert seat_recs[0]["feature_id"] == "f1"
    assert seat_recs[0]["attempt"] == 1
    assert seat_recs[0]["seat"] == "implement/claude"
    assert seat_recs[0]["resets"] == "12am (UTC)"
    esc = [r for r in recs if r["kind"] == "escalation.raised"]
    assert len(esc) == 1
    assert esc[0]["reason_class"] == "seat_exhausted"
    assert "implement/claude" in esc[0]["reason"]

    state, next_attempt, budget_used = run_mod._feature_state(recs, "g1", "f1")
    assert state == "escalated"


def test_seat_exhaustion_on_a_non_best_candidate_still_stops_the_run(monkeypatch, repo):
    good = _pipeline_episode(repo, "runs", "ep-best", _add_diff(repo))
    exhausted = _pipeline_episode(repo, "runs", "ep-other", _add_diff(repo),
                                  implement_log=_SEAT_MUST_MATCH[0])
    _prime(repo)
    monkeypatch.setattr(run_mod, "_build", lambda *a, **k: [good, exhausted])
    monkeypatch.setattr(run_mod, "best_episode", lambda cands: cands[0])

    code = run_mod._walk(_spec(pipeline=True), repo, "runs", 2, "")
    recs = ledger.read(repo)

    assert code == 1
    seat_recs = [r for r in recs if r["kind"] == "seat.exhausted"]
    assert len(seat_recs) == 1
    assert not any(r["kind"] == "feature.failed" for r in recs)
    assert not any(r["kind"] == "feature.landed" for r in recs)


def test_orchestrated_style_episode_with_a_limit_line_does_not_raise_seat_exhausted(
        monkeypatch, repo):
    """Repair-role logs under an orchestrated run are out of scope for this
    feature (a follow-up task covers them) — the scan must not even look."""
    ep_id = "ep-orch"
    out = repo / "runs" / ep_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "diff.patch").write_text(_add_diff(repo))
    _write_role_log(repo, "runs", ep_id, "repair", _SEAT_MUST_MATCH[0])
    ep = {"episode_id": ep_id, "outcome": "pass", "review_verdict": None,
          "roles": [{"role": "repair", "agent": "claude"}]}
    _prime(repo)
    monkeypatch.setattr(run_mod, "_build", lambda *a, **k: [ep])

    code = run_mod._walk(_spec(orchestrate=True), repo, "runs", 1, "")
    recs = ledger.read(repo)

    assert not any(r["kind"] == "seat.exhausted" for r in recs)
    assert not any(r.get("reason_class") == "seat_exhausted" for r in recs
                   if r["kind"] == "escalation.raised")
