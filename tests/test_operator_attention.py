"""My attention per landed feature (F29, DIRECTION §5): every human action is
an event with origin=operator, and insights counts them per landed feature.
An automatic approval is not attention and must not count."""
from unittest.mock import patch

from plexus import ledger, observe


def test_human_actions_are_operator_events_and_auto_approval_is_not(tmp_path):
    with patch.object(ledger.events, "emit") as emit:
        ledger.record("escalation.raised", goal_id="g", feature_id="f1", root=tmp_path)
        ledger.resolve(tmp_path, "g", "f1", "use the v2 client")
        ledger.record("plan.approved", goal_id="g", root=tmp_path,
                      approver="auto:unplanned-task", operator=False)
    origins = {c.args[0]: c.kwargs["origin"] for c in emit.call_args_list}
    assert origins == {"escalation.raised": None, "escalation.resolved": "operator",
                       "plan.approved": None}
    assert [r.get("operator", False) for r in ledger.read(tmp_path)] == [False, True, False]


def test_insights_reports_actions_per_landed_feature(tmp_path):
    with patch.object(ledger.events, "emit"):
        ledger.record("plan.approved", goal_id="g", root=tmp_path, operator=True)
        ledger.record("feature.landed", goal_id="g", feature_id="f1", root=tmp_path)
        ledger.record("escalation.raised", goal_id="g", feature_id="f2", root=tmp_path)
        ledger.resolve(tmp_path, "g", "f2", "ok")
        ledger.record("feature.landed", goal_id="g", feature_id="f2", root=tmp_path)
    (line,) = [l for l in observe.insights(str(tmp_path)) if l.startswith("attention:")]
    assert "2 operator action(s)" in line and "1.0 per landed feature" in line
    assert "plan.approved=1 escalation.resolved=1" in line


def test_no_operator_rows_no_attention_line(tmp_path):
    with patch.object(ledger.events, "emit"):
        ledger.record("feature.landed", goal_id="g", feature_id="f1", root=tmp_path)
    assert not [l for l in observe.insights(str(tmp_path)) if l.startswith("attention:")]
