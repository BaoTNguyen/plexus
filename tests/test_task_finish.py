"""What happens when a task's last feature lands: the queue's task is marked
landed (not the last episode's id), and the PR step only refreshes an OPEN PR."""
import subprocess

from plexus import ledger
from plexus import run as run_mod
from test_sandbox_start_retry import _FEAT, _add_diff, _episode, _make_repo, _spec, _write_plan


def _stub_heart(monkeypatch):
    monkeypatch.setattr(run_mod, "_episode_cost", lambda cands: {})
    monkeypatch.setattr(run_mod, "best_episode", lambda cands: cands[0])
    monkeypatch.setattr(run_mod.scope, "observe", lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "_run_acceptance", lambda *a, **k: (True, "", ""))


def test_finishing_a_task_marks_the_queue_task_landed(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    _stub_heart(monkeypatch)
    ep = _episode(repo, "runs", "ep-1", _add_diff(repo))
    _write_plan(repo, _FEAT, task_id="t1")
    ledger.record("plan.approved", goal_id="g1", root=repo, plan_id="")
    monkeypatch.setattr(run_mod, "_build", lambda *a, **k: [ep])
    updates = []
    monkeypatch.setattr(run_mod._tasks, "update",
                        lambda root, tid, **kw: updates.append((tid, kw)))

    assert run_mod._walk(_spec(), repo, "runs", 1, "t1") == 0
    assert ("t1", {"state": "landed"}) in updates, updates


def _gh(monkeypatch, view_stdout):
    """Stub git/gh: the PR step's subprocess calls, recorded."""
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        out = view_stdout if args[:3] == ["gh", "pr", "view"] else ""
        if args[:3] == ["gh", "pr", "create"]:
            out = "https://github.com/o/r/pull/9\n"
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(run_mod, "_git", lambda repo, *a: "dev" if a[0] == "rev-parse" else "origin")
    monkeypatch.setattr(run_mod, "report", lambda *a: "body")
    return calls


def test_a_merged_pr_is_not_edited_a_new_one_is_opened(monkeypatch, tmp_path):
    # gh pr view's -q filter drops a non-OPEN PR, so stdout is empty
    calls = _gh(monkeypatch, view_stdout="")
    note = run_mod._open_pr(_spec(pr_base="main"), tmp_path, str(tmp_path))
    assert not any(c[:3] == ["gh", "pr", "edit"] for c in calls)
    assert any(c[:3] == ["gh", "pr", "create"] for c in calls)
    assert "pull/9" in note
    view = next(c for c in calls if c[:3] == ["gh", "pr", "view"])
    assert "state" in " ".join(view) and "OPEN" in " ".join(view)


def test_an_open_pr_is_refreshed(monkeypatch, tmp_path):
    calls = _gh(monkeypatch, view_stdout="https://github.com/o/r/pull/7\n")
    note = run_mod._open_pr(_spec(pr_base="main"), tmp_path, str(tmp_path))
    assert any(c[:3] == ["gh", "pr", "edit"] for c in calls)
    assert not any(c[:3] == ["gh", "pr", "create"] for c in calls)
    assert "pull/7" in note
