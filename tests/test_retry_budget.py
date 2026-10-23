"""Attempts that can't help don't spend budget: a run that dies before its
episode settles (F83), and a third try at a regression that already failed
the same way twice (F22). Retries after a regression see its output (F24)."""
from plexus import ledger
from plexus import run as run_mod
from test_sandbox_start_retry import _FEAT, _add_diff, _episode, _make_repo, _spec, _write_plan


def _stub(monkeypatch):
    monkeypatch.setattr(run_mod, "_episode_cost", lambda cands: {})
    monkeypatch.setattr(run_mod, "best_episode", lambda cands: cands[0])
    monkeypatch.setattr(run_mod.scope, "observe", lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "_run_acceptance", lambda *a, **k: (True, "", ""))
    monkeypatch.setattr(run_mod._tasks, "update", lambda *a, **k: None)


def _regressed(repo, n, failed="tests/test_doctor.py::test_x"):
    ep = _episode(repo, "runs", f"ep-{n}", _add_diff(repo))
    ep["outcome"] = "fail"
    ep["verifier_results"] = {"pytest": {
        "passed": False, "output_tail": f"FAILED {failed} - boom\n1 failed in 0.{n}s"}}
    return ep


def test_a_start_with_no_settled_episode_spends_nothing():
    R = lambda **k: {"goal_id": "g", "feature_id": "f1", **k}  # noqa: E731
    recs = [R(kind="feature.started", attempt=1)]
    assert run_mod._feature_state(recs, "g", "f1") == ("open", 2, 0)
    recs += [R(kind="feature.started", attempt=2), R(kind="feature.failed", attempt=2)]
    assert run_mod._feature_state(recs, "g", "f1") == ("open", 3, 1)


def test_the_retry_sees_the_regression_output():
    tail = run_mod._verifier_tail({"verifier_results": {
        "pytest": {"passed": False, "output_tail": "FAILED t::x"}}})
    assert tail == "FAILED t::x"


def test_the_same_regression_twice_escalates_instead_of_a_third_try(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    _stub(monkeypatch)
    _write_plan(repo, _FEAT)
    ledger.record("plan.approved", goal_id="g1", root=repo, plan_id="")
    eps = iter([_regressed(repo, 1), _regressed(repo, 2), _regressed(repo, 3)])
    monkeypatch.setattr(run_mod, "_build", lambda *a, **k: [next(eps)])

    assert run_mod._walk(_spec(), repo, "runs", 1, "") == 1
    recs = ledger.read(repo)
    assert sum(r["kind"] == "feature.failed" for r in recs) == 2
    esc = [r for r in recs if r["kind"] == "escalation.raised"]
    assert [e["reason_class"] for e in esc] == ["regression_repeated"]
    assert "tests/test_doctor.py::test_x" in esc[0]["reason"]
    second = [r for r in recs if r["kind"] == "feature.started"][1]
    assert "FAILED tests/test_doctor.py::test_x" in second["retry_context"]["failure_tail"]


def test_a_different_regression_keeps_retrying(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path)
    _stub(monkeypatch)
    _write_plan(repo, _FEAT)
    ledger.record("plan.approved", goal_id="g1", root=repo, plan_id="")
    eps = iter([_regressed(repo, 1, "t::a"), _regressed(repo, 2, "t::b"),
                _regressed(repo, 3, "t::c")])
    monkeypatch.setattr(run_mod, "_build", lambda *a, **k: [next(eps)])

    assert run_mod._walk(_spec(), repo, "runs", 1, "") == 1
    recs = ledger.read(repo)
    assert sum(r["kind"] == "feature.failed" for r in recs) == 3
    assert [r["reason_class"] for r in recs if r["kind"] == "escalation.raised"] \
        == ["attempts_exhausted"]
