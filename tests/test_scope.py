"""The loop that turns a refused write into a better next attempt.

Heart emits sandbox.denied when a container refuses ground the spec permitted.
Before this, that reached the journal and nothing read it: the correction was
discarded when the journal rotated, and every attempt repeated the same
mis-prediction.

These pin the two rules that keep the loop honest -- distil while the events
still exist, and never let a probe become a permission.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from plexus import ledger, scope


@pytest.fixture
def root():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / ".plexus").mkdir()
        yield Path(tmp)


def _journal(tmp: Path, *events: dict) -> None:
    day = tmp / "20260825.ndjson"
    day.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _denied(task_id: str, *evidence: str) -> dict:
    return {"ts": "2026-08-25T00:00:00+00:00", "source": "heart",
            "kind": "sandbox.denied", "task_id": task_id,
            "payload": {"evidence": list(evidence)}}


def _probed(task_id: str, *paths: str) -> dict:
    return {"ts": "2026-08-25T00:00:00+00:00", "source": "heart",
            "kind": "guardrail.hit", "task_id": task_id,
            "payload": {"rules": ["denied_path_probe"], "paths": list(paths)}}


def _observe(root, tmp_journal, task_id="t1"):
    with patch.dict(os.environ, {"EVENT_JOURNAL_DIR": str(tmp_journal)}):
        return scope.observe(task_id, goal_id="g1", feature_id="f1", root=root)


def test_a_refusal_becomes_a_directory_the_next_attempt_can_write(root):
    """A spec granting one file refuses the next file the same task needs. The
    directory is the smallest widening likely to hold."""
    with tempfile.TemporaryDirectory() as j:
        _journal(Path(j), _denied("t1", "implement: Read-only file system: 'src/adapters/http.py'"))
        found = _observe(root, j)

    assert found["needed"] == ["src/adapters"]
    assert scope.for_task("t1", root=root)["allow"] == ["src/adapters"]


def test_a_probe_never_becomes_a_permission(root):
    """The reward hack this closes: reach past the boundary often enough and the
    boundary moves. A caught probe is evidence the prohibition works."""
    with tempfile.TemporaryDirectory() as j:
        _journal(Path(j), _probed("t1", "src/secrets"))
        _observe(root, j)

    learned = scope.for_task("t1", root=root)
    assert learned["deny"] == ["src/secrets"]
    assert learned["allow"] == [], "a probed path must never be granted"


def test_refusals_accumulate_across_attempts(root):
    """A task can be refused twice in different places, and the second refusal
    does not retract the first."""
    for path in ("src/adapters/http.py", "src/models/user.py"):
        with tempfile.TemporaryDirectory() as j:
            _journal(Path(j), _denied("t1", f"implement: EACCES: '{path}'"))
            _observe(root, j)

    assert scope.for_task("t1", root=root)["allow"] == ["src/adapters", "src/models"]


def test_one_task_does_not_learn_from_another(root):
    with tempfile.TemporaryDirectory() as j:
        _journal(Path(j), _denied("t1", "EACCES: 'src/a/x.py'"),
                 _denied("t2", "EACCES: 'src/b/y.py'"))
        _observe(root, j, task_id="t1")

    assert scope.for_task("t1", root=root)["allow"] == ["src/a"]
    assert scope.for_task("t2", root=root)["allow"] == []


def test_a_quiet_episode_records_nothing(root):
    with tempfile.TemporaryDirectory() as j:
        _journal(Path(j), {"ts": "2026-08-25T00:00:00+00:00", "source": "heart",
                           "kind": "episode.finished", "task_id": "t1", "payload": {}})
        assert _observe(root, j) is None

    assert [r for r in ledger.read(root) if r.get("kind") == "scope.observed"] == []


def test_a_missing_journal_is_not_a_failure(root):
    """Best-effort by contract: heart may be absent, or the journal rotated.
    Neither should fail a goal."""
    with patch("heart.pulse.load_events", side_effect=RuntimeError("no journal")):
        assert scope.observe("t1", goal_id="g1", root=root) is None


def test_the_distillation_survives_the_journal(root):
    """The reason this reads once and writes to the ledger. A journal scan at
    plexus's horizon returns less the older the question gets, which looks like
    the system having stopped making mistakes."""
    with tempfile.TemporaryDirectory() as j:
        _journal(Path(j), _denied("t1", "EACCES: 'src/adapters/http.py'"))
        _observe(root, j)
    # journal directory is gone entirely at this point

    assert scope.for_task("t1", root=root)["allow"] == ["src/adapters"]
