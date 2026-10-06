"""review_rejected -> retry_context threading. Stubs heart's episode building
entirely (_build_sandbox_retrying/best_episode), so the walk runs against a
scratch git repo under tmp_path with no sandbox, no suite framework, nothing
heart would need a container or network for.

Covers: after a review_rejected attempt, the next feature.started's
retry_context carries the reviewer's findings (severity, file, line, claim)
for that feature, keyed under their own entry rather than overwriting
whatever else retry_context might carry (see run.py's blocked_on_decision
resume payload, the existing mechanism this reuses).
"""
import json
import os
import subprocess

from plexus import ledger
from plexus import vascular_state
from plexus import run as _run
from plexus.spec import load_spec

FINDING = {"severity": "blocker", "file": "src/two.py", "line": 1,
           "claim": "no error handling"}
DIFF = ("diff --git a/src/two.py b/src/two.py\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/src/two.py\n@@ -0,0 +1 @@\n+x = 1\n")


def test_review_rejected_feeds_findings_into_next_attempts_retry_context(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # nothing here writes relative paths outside it
    monkeypatch.setenv("EVENT_JOURNAL_DIR", str(tmp_path / "journal"))

    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "config", "user.name", "t"], check=True)
    subprocess.run([*git, "config", "user.email", "t@t"], check=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "seed"], check=True)

    # network="api" so no reviewer role is expected (pipeline off, so the
    # review_rejected verdict comes purely from our stubbed episode); attempts
    # capped at 2 so the walk escalates attempts_exhausted right after the
    # second feature.started instead of running the land/task-board
    # machinery, which is not what this test is about
    (repo / "plexus.toml").write_text(
        '[goal]\nid="g1"\ntext="t"\n[ground_truth]\nsuite="true"\n'
        '[agent]\nnetwork="api"\n[budgets]\nattempts_per_feature=2\n')
    spec = load_spec(repo)

    plan_dir = vascular_state.plexus_dir(repo)
    plan_dir.mkdir(parents=True)
    feat = {"plan_id": "p1", "id": "f1", "title": "f1", "spec": "do the thing",
            "acceptance": "true", "touches": ["src/two.py"], "contract": []}
    (plan_dir / "plan.jsonl").write_text(json.dumps(feat) + "\n")
    ledger.record("plan.approved", goal_id="g1", root=repo, plan_id="p1")

    episodes_built: list[dict] = []

    def fake_build(spec_, task, candidates, roles, runs_dir, goal_id,
                   feature_id, attempt, root):
        ep_id = f"ep{len(episodes_built)}"
        out = runs_dir / ep_id
        out.mkdir(parents=True, exist_ok=True)
        (out / "diff.patch").write_text(DIFF)
        if not episodes_built:
            ep = {"episode_id": ep_id, "outcome": "pass",
                  "review_verdict": "reject", "review_findings": [FINDING]}
        else:
            # fails for an unrelated reason, purely to end the walk short of
            # landing -- this test is only about what retry_context carries
            ep = {"episode_id": ep_id, "outcome": "fail", "review_verdict": None}
        episodes_built.append(ep)
        return [ep]

    monkeypatch.setattr(_run, "_build_sandbox_retrying", fake_build)
    monkeypatch.setattr(_run, "best_episode", lambda cands: cands[0])

    rc = _run.run(spec, root=repo, runs_dir="runs", candidates=1)
    recs = ledger.read(repo)
    assert rc == 1, recs  # attempts_exhausted after the 2-attempt budget, by design
    assert len(episodes_built) == 2, "expected a rejected attempt then a failed one"
    assert any(r["kind"] == "escalation.raised"
               and r.get("reason_class") == "attempts_exhausted" for r in recs), recs
    assert not any(r["kind"] == "feature.landed" for r in recs), recs

    starts = [r for r in recs if r["kind"] == "feature.started" and r["feature_id"] == "f1"]
    assert len(starts) == 2, starts
    first_ctx, second_ctx = starts[0]["retry_context"], starts[1]["retry_context"]
    assert first_ctx == {}, "nothing to retry from on the first attempt"

    findings = second_ctx["review_findings"]
    assert findings == [FINDING], second_ctx
    assert findings[0]["severity"] == "blocker"
    assert findings[0]["file"] == "src/two.py"
    assert findings[0]["line"] == 1
    assert findings[0]["claim"] == "no error handling"
