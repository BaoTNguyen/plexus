"""F13: acceptance criteria broken on their face are refused before a run.

Every plexus plan since 09-28 needed `cd /work` removed by hand, and some
shipped guards that turn a missing tool into a pass."""
import json

import pytest

from plexus import vascular_state
from plexus.plan import approve, criterion_problems
from plexus.spec import GoalSpec

# real criteria from past plans, and look-alikes that must stay clean
BROKEN = [
    "cd /work && python3 -m pytest -q tests/test_plexus.py tests/test_review.py",
    "cd /work/sub && pytest",
    "(command -v openssl >/dev/null || exit 0) && python3 -m pytest -q tests/t.py",
    "python3 -m pytest -q tests/x.py || true",
    "(pytest -q || :) && echo done",
    "cat /journal/x.ndjson",
]
CLEAN = [
    "python3 -m pytest -q tests/test_new.py && python3 -m pytest -q",
    "cargo test --locked paths",
    "test -f src/work/x.py && grep -q exit src/a.py",
    "ls ./work/x && cat docs/deps/a",
    "grep -q foo x.txt || exit 1",
    "python3 -c 'import sys; sys.exit(0 if ok else 1)'",
]


@pytest.mark.parametrize("cmd", BROKEN)
def test_a_broken_criterion_is_named(cmd):
    assert criterion_problems(cmd), cmd


@pytest.mark.parametrize("cmd", CLEAN)
def test_a_sound_criterion_is_left_alone(cmd):
    assert criterion_problems(cmd) == [], cmd


def test_approve_refuses_a_broken_criterion_even_when_waived(tmp_path):
    plan = vascular_state.plexus_dir(tmp_path) / "plan.jsonl"
    plan.parent.mkdir(parents=True)
    plan.write_text(json.dumps({"plan_id": "p1", "task_id": "", "id": "f1",
                                "acceptance": "cd /work && pytest -q"}) + "\n")
    spec = GoalSpec(goal_id="g", text="t", context="", suite="true",
                    attempts_per_feature=1, episodes_per_goal=5, agent="claude",
                    agent_cmd=None, timeout=30, spec_hash="h")
    with pytest.raises(SystemExit, match="f1: names /work"):
        approve(spec, tmp_path, waive=True)
