"""One rule for "is this escalation open", asked by status, the dashboard,
resolve, the run loop and prune. Four hand-rolled copies had drifted:
PLEXUS-B03 (a double resolve went negative and hid the next block),
B08 (a goal-level regression could be neither seen nor resolved) and B10
(prune deleted the episodes of a block raised again after a resolve)."""

from plexus import ledger, observe, prune, run


def _esc(kind, feature=None, **kw):
    r = {"kind": kind, "goal_id": "g", **kw}
    if feature:
        r["feature_id"] = feature
    return r


RAISE, RESOLVE = "escalation.raised", "escalation.resolved"


def test_a_double_resolve_does_not_cancel_the_next_block():
    recs = [_esc(RAISE, "f1"), _esc(RESOLVE, "f1"), _esc(RESOLVE, "f1"),
            _esc(RAISE, "f1", reason="second question")]
    (e,) = ledger.open_escalations(recs, "g")
    assert (e["feature_id"], e["reason"]) == ("f1", "second question")
    assert run._feature_state(recs, "g", "f1")[0] == "escalated"


def test_a_goal_level_regression_is_visible_and_resolvable(tmp_path):
    ledger.record(RAISE, goal_id="g", root=tmp_path, reason_class="regression",
                  reason="suite failed", episode_ids=["E9"])
    (e,) = ledger.open_escalations(ledger.read(tmp_path), "g")
    assert e["feature_id"] == ledger.GOAL_LEVEL
    assert observe.status(str(tmp_path))[1] == 1

    ledger.record(RESOLVE, goal_id="g", feature_id=ledger.GOAL_LEVEL, root=tmp_path)
    assert ledger.open_escalations(ledger.read(tmp_path), "g") == []
    assert observe.status(str(tmp_path))[1] != 1


def test_prune_keeps_the_episodes_of_a_block_raised_again():
    recs = [_esc(RAISE, "f1", episode_ids=["E1"]), _esc(RESOLVE, "f1"),
            _esc(RAISE, "f1", episode_ids=["E2"])]
    kept = prune._referenced(recs)
    assert "E2" in kept, "the open escalation's episode would be pruned"
    assert "E1" not in kept


def test_status_counts_open_blocks_not_raises_minus_resolves(tmp_path):
    for kind in (RAISE, RESOLVE, RESOLVE):
        ledger.record(kind, goal_id="g", feature_id="f1", root=tmp_path)
    ledger.record(RAISE, goal_id="g", feature_id="f2", root=tmp_path, reason="why")
    lines, code = observe.status(str(tmp_path))
    assert code == 1 and "1 escalation(s) open" in lines[0] and "f2" in lines[0], lines


def test_latest_raise_comes_last_and_other_goals_do_not_leak():
    recs = [_esc(RAISE, "f1"), _esc(RAISE, "f2"), _esc(RAISE, "f1", reason="again"),
            {"kind": RAISE, "goal_id": "other", "feature_id": "f9"}]
    assert [e["feature_id"] for e in ledger.open_escalations(recs, "g")] == ["f2", "f1"]


def test_resolve_refuses_a_block_that_is_not_open(tmp_path):
    ledger.record(RAISE, goal_id="g", root=tmp_path, reason_class="regression")
    assert ledger.resolve(tmp_path, "g", "f-typo", "x") == [ledger.GOAL_LEVEL]
    assert ledger.resolve(tmp_path, "g", ledger.GOAL_LEVEL, "fixed the suite") is None
    assert ledger.resolve(tmp_path, "g", ledger.GOAL_LEVEL, "second tab") == []
    assert sum(r["kind"] == RESOLVE for r in ledger.read(tmp_path)) == 1
