"""plexus's per-repo state lands under <root>/.vascular/plexus, never <root>/.plexus.

Runs under pytest only: everything happens in tmp_path, no git, no network.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from plexus import ledger, plan, review, tasks, vascular_state
from plexus.spec import GoalSpec


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # heart's event journal and anything else outside the repo stay in tmp too
    monkeypatch.setenv("VASCULAR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EVENT_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.delenv("HEART_SANDBOX", raising=False)
    r = tmp_path / "repo"
    r.mkdir()
    return r


def test_tasks_ledger_plan_write_under_vascular(root, monkeypatch):
    state = root / ".vascular" / "plexus"
    assert tasks.tasks_path(root) == state / "tasks.jsonl"
    assert ledger.ledger_path(root) == state / "ledger.jsonl"
    assert plan.plan_path(root) == state / "plan.jsonl"
    assert plan.plan_path(root, "t1") == state / "plans" / "t1.jsonl"

    task = tasks.create(root, "do the thing")
    ledger.record("test.kind", goal_id="g", root=root)

    feats = [{"id": "f1", "title": "one", "spec": "s", "acceptance": "true",
              "touches": ["src/x.py"], "contract": [], "priority": 0, "depends_on": []}]

    def fake_agent(agent, prompt, *, log_path, **kw):
        log_path.write_text(json.dumps(feats), encoding="utf-8")
        return {"exit_code": 0}

    monkeypatch.setattr(plan, "run_agent", fake_agent)
    spec = SimpleNamespace(text="goal", context="", suite="true", manual_checks=[],
                           goal_id="g", timeout=5, network="none", agent="claude",
                           agent_cmd=None, spec_hash="h")
    plan.make_plan(spec, root)
    plan.make_plan(spec, root, task_id=task["id"])

    assert (state / "tasks.jsonl").is_file()
    assert (state / "ledger.jsonl").is_file()
    assert (state / "plan.jsonl").is_file()
    assert (state / "plans" / f"{task['id']}.jsonl").is_file()
    assert (state / "plan.log").is_file()
    assert not (root / ".plexus").exists()
    assert sorted(p.name for p in root.iterdir()) == [".vascular"]


def test_runs_dir_under_vascular(root):
    runs = vascular_state.runs_dir(root)
    assert runs == root / ".vascular" / "plexus" / "runs"
    runs.mkdir(parents=True)
    (runs / "r1.json").write_text("{}")
    assert not (root / "runs").exists()
    assert not (root / ".plexus").exists()


def test_vascular_hook_diff_classifies_exec(root):
    path = ".vascular/arteries/hooks/x.sh"
    diff = (f"diff --git a/{path} b/{path}\nnew file mode 100755\n--- /dev/null\n"
            f"+++ b/{path}\n@@ -0,0 +1 @@\n+echo hi\n")
    paths = list(review._diff_file_patches(diff))
    assert paths == [path]
    assert any(review._matches(path, g) for g in review.EXEC_DIRS)
    assert review.exec_surface(paths) == [path]

    # run.py's land-time rule: plan class from classify(), raised to exec when
    # the diff touches an exec surface and the plan class isn't already held
    feat = {"id": "f", "touches": [path], "contract": []}
    cls = review.classify(feat)
    hold = GoalSpec.__dataclass_fields__["review_hold"].default
    if cls not in hold:
        cls = "exec" if review.exec_surface(paths) else cls
    assert cls == "exec"
    assert cls in review.CLASSES and cls in hold
