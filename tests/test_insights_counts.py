"""attempt_outcomes settles every feature.started exactly once, and insights /
report count from it: six buckets per task, cost billed once per attempt."""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["EVENT_JOURNAL_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plexus import observe  # noqa: E402


def _write(root: Path, recs: list[dict]) -> str:
    path = root / ".vascular" / "plexus" / "ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    for i, r in enumerate(recs):
        r.setdefault("ts", f"2026-01-01T00:00:{i:02d}+00:00")
        r.setdefault("goal_id", "g")
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return str(root)


def _start(fid, n):
    return {"kind": "feature.started", "feature_id": fid, "attempt": n}


def _esc(fid, cls, cost, **kw):
    return {"kind": "escalation.raised", "feature_id": fid, "reason_class": cls,
            "cost_usd": cost, **kw}


LEDGER = [
    {"kind": "goal.started"},
    _start("t1:a", 1), {"kind": "feature.failed", "feature_id": "t1:a", "cost_usd": 0.5},
    _start("t1:a", 2), {"kind": "feature.landed", "feature_id": "t1:a", "cost_usd": 1.0},
    _start("t1:b", 1), _esc("t1:b", "held_for_review", 0.25, episode_ids=["ep-b"]),
    _start("t2:c", 1), _esc("t2:c", "blocked_on_decision", 0.5),
    _start("t2:c", 2),
    _start("t2:d", 1), _esc("t2:d", "scope_violation", 0.1),
]

LINES = [
    "attempts: started=6 landed=1 failed=1 blocked=1 held=1 scope=1 crashed=1 cost=$2.3500",
    "  task t1: started=3 landed=1 failed=1 blocked=0 held=1 scope=0 crashed=0 cost=$1.7500",
    "  task t2: started=3 landed=0 failed=0 blocked=1 held=0 scope=1 crashed=1 cost=$0.6000",
]


def _ledger():
    return [dict(r) for r in LEDGER]


def test_counts(tmp_path):
    root = _write(tmp_path, _ledger())
    rows = observe.attempt_outcomes(observe.read(root))
    got = [(r["task"], r["feature_id"], r["attempt"], r["outcome"], r["cost_usd"]) for r in rows]
    assert got == [("t1", "t1:a", 1, "failed", 0.5), ("t1", "t1:a", 2, "landed", 1.0),
                   ("t1", "t1:b", 1, "held", 0.25), ("t2", "t2:c", 1, "blocked", 0.5),
                   ("t2", "t2:c", 2, "crashed", None), ("t2", "t2:d", 1, "scope", 0.1)], got
    for task, spend in (("t1", 1.75), ("t2", 0.6)):
        assert round(sum(r["cost_usd"] or 0 for r in rows if r["task"] == task), 4) == spend
    out = observe.insights(root)
    for line in LINES:
        assert line in out, out
    assert any(line.startswith("cost: $2.3500") for line in out), out


def test_held_then_landed(tmp_path):
    root = _write(tmp_path, _ledger() + [
        {"kind": "escalation.resolved", "feature_id": "t1:b"},
        {"kind": "feature.landed", "feature_id": "t1:b", "from_held": "ep-b"},
        # a from_held naming no known hold still settles the open attempt
        _start("t2:e", 1),
        {"kind": "feature.landed", "feature_id": "t2:e", "from_held": "ep-x", "cost_usd": 0.2},
    ])
    rows = observe.attempt_outcomes(observe.read(root))
    b = [r for r in rows if r["feature_id"] == "t1:b"]
    assert [(r["outcome"], r["cost_usd"]) for r in b] == [("landed", 0.25)], b
    e = [r for r in rows if r["feature_id"] == "t2:e"]
    assert [(r["outcome"], r["cost_usd"]) for r in e] == [("landed", 0.2)], e
    out = observe.insights(root)
    assert ("  task t1: started=3 landed=2 failed=1 blocked=0 held=0 scope=0 crashed=0 "
            "cost=$1.7500") in out, out


def test_legacy_and_orphans(tmp_path):
    root = _write(tmp_path, [
        _start("solo", 1),  # no ':' -> task (none)
        {"kind": "escalation.raised", "feature_id": "solo", "reason_class": "held_stale",
         "held_cost": {"cost_usd": 0.3, "tokens_in": 9, "tokens_out": 4}},
        _start("t3:f", 1), {"kind": "feature.failed", "feature_id": "t3:f"},  # unpriced
        # nothing open for t3:f: settles nothing, bills nothing
        _esc("t3:f", "attempts_exhausted", 9.0),
    ])
    rows = observe.attempt_outcomes(observe.read(root))
    assert [(r["task"], r["outcome"], r["cost_usd"], r["tokens_in"]) for r in rows] == [
        ("(none)", "held", 0.3, 9), ("t3", "failed", None, None)], rows
    assert ("  task (none): started=1 landed=0 failed=0 blocked=0 held=1 scope=0 crashed=0 "
            "cost=$0.3000") in observe.insights(root)


def test_report_attempts(tmp_path):
    root = _write(tmp_path, _ledger())
    out = observe.report([root])
    assert out[0].split()[-3:] == ["esc/feat", "attempts", "landed"], out
    row = next(line for line in out if line.startswith("g "))
    assert row.split()[-2:] == ["6", "1"], out  # attempts, landed


if __name__ == "__main__":
    print("\n".join(observe.insights(_write(Path(tempfile.mkdtemp()), _ledger()))))
    for fn in (test_counts, test_held_then_landed, test_legacy_and_orphans,
               test_report_attempts):
        fn(Path(tempfile.mkdtemp()))
    print("ok")
