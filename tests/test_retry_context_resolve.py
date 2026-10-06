"""After an escalation is resolved, the next attempt's retry_context must carry
the resolve answer — for every escalation class run.py raises, not just
blocked_on_decision. Stdlib asserts, no frameworks, no docker, no network.
Run: python3 tests/test_retry_context_resolve.py (or via pytest).
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

tmp = Path(tempfile.mkdtemp(prefix="plexus-retry-context-test-"))
os.environ["EVENT_JOURNAL_DIR"] = str(tmp / "journal")

here = Path(__file__).resolve()
for p in (here.parents[1] / "src", here.parents[2] / "heart" / "src"):
    sys.path.insert(0, str(p))

from plexus import ledger  # noqa: E402
from plexus.run import _resume_answer  # noqa: E402

REASON_CLASSES = [
    "blocked_on_decision",
    "attempts_exhausted",
    "held_for_review",
    "sandbox_unavailable",
]


@pytest.mark.parametrize("reason_class", REASON_CLASSES)
def test_resolve_answer_carries_into_retry_context(tmp_path, reason_class):
    root = tmp_path / "repo"
    goal_id, feature_id = "g1", "f1"
    question = f"what about {reason_class}?"
    answer = f"go ahead, {reason_class} is fine"

    ledger.record("escalation.raised", goal_id=goal_id, feature_id=feature_id,
                  root=root, reason_class=reason_class, reason=question,
                  episode_ids=[])
    ledger.record("escalation.resolved", goal_id=goal_id, feature_id=feature_id,
                  root=root, resolution=answer)

    recs = ledger.read(root)
    resume_answer = _resume_answer(recs, goal_id, feature_id)
    retry_context = {"resume_answer": resume_answer} if resume_answer else {}

    assert retry_context.get("resume_answer"), reason_class
    assert question in retry_context["resume_answer"]
    assert answer in retry_context["resume_answer"]


if __name__ == "__main__":
    for rc in REASON_CLASSES:
        test_resolve_answer_carries_into_retry_context(
            tmp_path=Path(tempfile.mkdtemp(prefix="plexus-retry-context-")), reason_class=rc)
    print("ok")
