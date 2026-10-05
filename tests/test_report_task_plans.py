import json
from types import SimpleNamespace
from plexus import ledger, review


def test_report_reads_task_plans_without_project_plan(tmp_path):
    # task repos have .plexus/plans/<task>.jsonl and no .plexus/plan.jsonl
    plans = tmp_path / ".plexus" / "plans"
    plans.mkdir(parents=True)
    (plans / "t.jsonl").write_text(json.dumps(
        {"id": "t:f", "title": "feat", "touches": ["a.py"], "acceptance": "true", "spec": "s"}) + "\n")
    ledger.record("feature.landed", goal_id="g", root=tmp_path, feature_id="t:f",
                  attempt=1, commit="")
    out = review.report(SimpleNamespace(goal_id="g"), tmp_path)
    assert "t:f" in out
