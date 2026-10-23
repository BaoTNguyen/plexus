from plexus.run import _goal_spend


def _recs():
    return [
        {"kind": "feature.started", "feature_id": "t1:a"},
        {"kind": "feature.failed", "feature_id": "t1:a", "cost_usd": 0.5,
         "tokens_in": 10, "tokens_out": 1},
        {"kind": "feature.started", "feature_id": "t1:a"},
        {"kind": "feature.landed", "feature_id": "t1:a", "cost_usd": 0.25,
         "tokens_in": 20, "tokens_out": 2},
        {"kind": "feature.started", "feature_id": "t2:b"},
        {"kind": "feature.landed", "feature_id": "t2:b", "cost_usd": 2.0,
         "tokens_in": 300, "tokens_out": 30},
        # 't10' shares the 't1' prefix but not the 't1:' namespace
        {"kind": "feature.started", "feature_id": "t10:c"},
        {"kind": "feature.landed", "feature_id": "t10:c", "cost_usd": 4.0},
        # goal-level record with no feature_id: whole-ledger only
        {"kind": "escalation.raised", "cost_usd": 8.0},
        {"kind": "goal.finished", "outcome": "scope_satisfied"},
    ]


def test_task_scopes_spend():
    s = _goal_spend(_recs(), "t1")
    assert s == {"episodes_total": 2, "spend_usd": 0.75,
                 "spend_tokens_in": 30, "spend_tokens_out": 3}
    assert _goal_spend(_recs(), "t2")["episodes_total"] == 1
    assert _goal_spend(_recs(), "t2")["spend_usd"] == 2.0
    # an unpriced task omits spend keys rather than reporting zero
    assert _goal_spend(_recs(), "t9") == {"episodes_total": 0}


def test_no_task_counts_whole_ledger():
    s = _goal_spend(_recs())
    assert s["episodes_total"] == 4
    assert s["spend_usd"] == 14.75
    assert s["spend_tokens_in"] == 330
    assert _goal_spend(_recs(), "") == s
