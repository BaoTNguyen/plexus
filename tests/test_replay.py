"""plexus replay (F28): how a sample is scored from its clone's ledger, and how
the set is filtered. The runs themselves are paid episodes; these check the
arithmetic that turns them into a pass rate."""
import json

from plexus import replay


def _r(kind, ts, **kw):
    return {"kind": kind, "ts": f"2026-10-23T10:{ts}:00+00:00", "goal_id": "g", **kw}


def test_a_landed_retry_counts_every_attempt_and_its_cost():
    recs = [_r("feature.started", "00"), _r("feature.failed", "05", cost_usd=0.4),
            _r("feature.started", "06"), _r("feature.landed", "15", cost_usd=0.6)]
    assert replay.outcome(recs) == {"outcome": "landed", "reason_class": None, "attempts": 2,
                                    "cost_usd": 1.0, "lead_s": 900.0}


def test_an_escalation_is_not_a_pass_and_keeps_its_class():
    recs = [_r("feature.started", "00"), _r("escalation.raised", "09", reason_class="intent",
                                             cost_usd=0.5)]
    got = replay.outcome(recs)
    assert (got["outcome"], got["reason_class"], got["cost_usd"], got["lead_s"]) == \
        ("escalated", "intent", 0.5, None)


def test_summary():
    lines = replay.summary([
        {"outcome": "landed", "cost_usd": 1.0, "lead_s": 600},
        {"outcome": "landed", "cost_usd": 3.0, "lead_s": 1200},
        {"outcome": "escalated", "cost_usd": 2.0, "lead_s": None}])
    assert lines == ["pass rate: 2/3 (67%)", "cost: $6.00 total, $2.00 median per sample",
                     "lead time (landed): p50 15.0 min, max 20.0 min"]


def test_holdout_is_kept_out_unless_asked(tmp_path):
    p = tmp_path / "set.json"
    p.write_text(json.dumps({"tasks": [{"id": "a"}, {"id": "b", "holdout": True}]}))
    assert [t["id"] for t in replay.load(p)] == ["a"]
    assert [t["id"] for t in replay.load(p, holdout=True)] == ["b"]
    assert [t["id"] for t in replay.load(p, only=["b"])] == ["b"]


def test_the_shipped_set_is_well_formed():
    tasks = json.loads((replay.Path(__file__).resolve().parents[1] / "evals" / "replay.json")
                       .read_text())["tasks"]
    assert len({t["id"] for t in tasks}) == len(tasks)
    for t in tasks:
        assert len(t["base"]) == len(t["landed"]) == 40, t["id"]
        assert t["feature"]["acceptance"] and t["feature"]["title"], t["id"]


def test_a_clone_has_no_remote_to_deliver_to(tmp_path, monkeypatch):
    """A finished goal pushes its branch to origin; a replay's origin would be
    the real checkout."""
    import subprocess
    src = tmp_path / "src"
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    head = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    monkeypatch.setattr(replay, "_repo", lambda task: src)
    work = replay._clone({"repo": "src"}, tmp_path / "out", head)
    assert subprocess.run(["git", "-C", str(work), "remote"], capture_output=True,
                          text=True).stdout.strip() == ""
