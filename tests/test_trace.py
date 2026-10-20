import hashlib
import json
import os
import re
import subprocess

import pytest

from plexus import events, ledger, run as run_mod, vascular_state
from plexus.spec import GoalSpec

HEX = re.compile(r"^[0-9a-f]+$")
SPAN_KEYS = ("span_id", "trace_id", "parent_span_id")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENT_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("VASCULAR_HOME", str(tmp_path / "vascular"))
    monkeypatch.chdir(tmp_path)


def _journal_line(tmp_path, kind):
    for f in (tmp_path / "journal").glob("**/*"):
        if f.is_file():
            for line in f.read_text().splitlines():
                if line.strip():
                    ev = json.loads(line)
                    if ev.get("kind") == kind:
                        return ev
    raise AssertionError(f"no {kind} event in journal")


def test_ids_deterministic():
    t = events.trace_for("план")
    s = events.attempt_span("план", "f1", 1)
    assert t == events.trace_for("план")
    assert s == events.attempt_span("план", "f1", 1)
    assert t == hashlib.sha256("plexus:план".encode("utf-8")).hexdigest()[:32]
    assert s == hashlib.sha256(
        "plexus:план:f1:1".encode("utf-8")
    ).hexdigest()[:16]
    assert len(t) == 32 and len(s) == 16
    assert HEX.fullmatch(t) and HEX.fullmatch(s)
    assert t != events.trace_for("p2")
    assert s != events.attempt_span("p2", "f1", 1)
    assert s != events.attempt_span("план", "f2", 1)
    assert s != events.attempt_span("план", "f1", 2)


def test_emit_span_fields_top_level(tmp_path):
    events.emit("x.started", span_id="s", trace_id="t", parent_span_id="p")
    ev = _journal_line(tmp_path, "x.started")
    assert (ev["span_id"], ev["trace_id"], ev["parent_span_id"]) == ("s", "t", "p")
    payload = ev.get("payload") or {}
    assert not any(k in payload for k in SPAN_KEYS)


def test_ledger_record_forwards_span(tmp_path):
    rec = ledger.record("feature.started", goal_id="g", feature_id="f",
                        root=tmp_path, attempt=1, span_id="s", trace_id="t")
    assert rec["span_id"] == "s" and rec["trace_id"] == "t"
    ev = _journal_line(tmp_path, "feature.started")
    assert ev["span_id"] == "s" and ev["trace_id"] == "t"
    payload = ev.get("payload") or {}
    assert not any(k in payload for k in SPAN_KEYS)


# --- run() threads the plan's trace and each attempt's span ---------------

PLAN_ID = "p1"
FID = "f1"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "a")
    (repo / "README.md").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    (vascular_state.plexus_dir(repo)).mkdir(parents=True, exist_ok=True)
    (vascular_state.plexus_dir(repo) / "plan.jsonl").write_text(json.dumps(
        {"id": FID, "title": "t", "spec": "do a thing", "acceptance": "true",
         "touches": None, "plan_id": PLAN_ID}) + "\n")
    ledger.record("plan.approved", goal_id="g1", root=repo, plan_id=PLAN_ID)
    return repo


def _diff(repo) -> str:
    (repo / "foo.txt").write_text("hello\n")
    _git(repo, "add", "foo.txt")
    diff = _git(repo, "diff", "--cached")
    _git(repo, "reset", "-q", "--")
    (repo / "foo.txt").unlink()
    return diff


def _spec(**over):
    base = dict(goal_id="g1", text="t", context="", suite="true",
                attempts_per_feature=3, episodes_per_goal=25, agent="claude",
                agent_cmd=None, timeout=60, spec_hash="deadbeef", pipeline=False,
                network="api", review_hold=(), pr_base="")
    base.update(over)
    return GoalSpec(**base)


@pytest.fixture
def stub_run(tmp_path, monkeypatch):
    monkeypatch.delenv("PULSE_TRACE_ID", raising=False)
    monkeypatch.delenv("PULSE_PARENT_SPAN", raising=False)
    monkeypatch.setattr(run_mod, "_episode_cost", lambda cands: {})
    monkeypatch.setattr(run_mod, "best_episode", lambda cands: cands[0])
    monkeypatch.setattr(run_mod.scope, "observe", lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "_open_pr", lambda *a, **k: "")
    repo = _repo(tmp_path)
    diff = _diff(repo)
    seen = []

    def dispatch(task, candidates, **kwargs):
        seen.append((os.environ.get("PULSE_TRACE_ID"),
                     os.environ.get("PULSE_PARENT_SPAN")))
        ep_id = f"ep-{len(seen)}"
        (repo / "runs" / ep_id).mkdir(parents=True)
        (repo / "runs" / ep_id / "diff.patch").write_text(diff)
        return [{"episode_id": ep_id, "outcome": "pass",
                 "review_verdict": None, "roles": []}]

    monkeypatch.setattr(run_mod, "run_candidates", dispatch)
    return repo, seen


def _journal(tmp_path, kind):
    out = []
    for f in (tmp_path / "journal").glob("**/*"):
        if f.is_file():
            out += [ev for ev in map(json.loads, filter(str.strip,
                                                         f.read_text().splitlines()))
                    if ev.get("kind") == kind]
    return out


def test_run_lands_feature_with_attempt_span(tmp_path, monkeypatch, stub_run):
    repo, seen = stub_run
    monkeypatch.setattr(run_mod, "_run_acceptance", lambda *a, **k: (True, "", ""))
    assert run_mod.run(_spec(), root=repo, runs_dir="runs") == 0
    span = events.attempt_span(PLAN_ID, FID, 1)
    trace = events.trace_for(PLAN_ID)
    for kind in ("feature.started", "feature.landed"):
        (ev,) = _journal(tmp_path, kind)
        assert (ev["span_id"], ev["trace_id"]) == (span, trace)
    assert seen == [(trace, span)]
    assert "PULSE_TRACE_ID" not in os.environ
    assert "PULSE_PARENT_SPAN" not in os.environ


def test_failed_attempt_carries_span(tmp_path, monkeypatch, stub_run):
    repo, seen = stub_run
    verdicts = iter([(False, "boom", "acceptance"), (True, "", "")])
    monkeypatch.setattr(run_mod, "_run_acceptance", lambda *a, **k: next(verdicts))
    assert run_mod.run(_spec(), root=repo, runs_dir="runs") == 0
    (failed,) = _journal(tmp_path, "feature.failed")
    assert failed["payload"]["attempt"] == 1
    assert failed["span_id"] == events.attempt_span(PLAN_ID, FID, 1)
    assert failed["trace_id"] == events.trace_for(PLAN_ID)
    (landed,) = _journal(tmp_path, "feature.landed")
    assert landed["span_id"] == events.attempt_span(PLAN_ID, FID, 2)


def test_trace_env_restored_on_raise(tmp_path, monkeypatch, stub_run):
    repo, _ = stub_run
    monkeypatch.setenv("PULSE_TRACE_ID", "prev")
    monkeypatch.setenv("PULSE_PARENT_SPAN", "parent-prev")

    def boom(*a, **k):
        assert os.environ["PULSE_TRACE_ID"] == events.trace_for(PLAN_ID)
        assert os.environ["PULSE_PARENT_SPAN"] == events.attempt_span(PLAN_ID, FID, 1)
        raise ValueError("stub dispatch blew up")

    monkeypatch.setattr(run_mod, "run_candidates", boom)
    with pytest.raises(ValueError):
        run_mod.run(_spec(), root=repo, runs_dir="runs")
    assert os.environ["PULSE_TRACE_ID"] == "prev"
    assert os.environ["PULSE_PARENT_SPAN"] == "parent-prev"
